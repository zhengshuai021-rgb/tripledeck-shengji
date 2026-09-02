#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三副牌升级 · Web 版 v1.2 — Flask 后端

状态机:
  idle → trump → bury → stir → playing → settled → (next round or finish)

每 step() 推进一个"视觉步骤"(一整圈出牌), 前端逐步/自动播放。
"""

import os
import sys
import time
import random
import io
from datetime import datetime
from flask import Flask, render_template, jsonify, request, send_file

sys.path.insert(0, os.path.dirname(__file__))

from game import (
    create_deck, Card, Bot, RoundRecord,
    SUITS, SUIT_CN, SCORE_RANKS, SCORE_VALUES, RANK_ORDER,
    is_main, cp, cards_str, classify, compare_plays,
    determine_trump, determine_stir, settle_round, PATTERN_CN, ACTION_CN, label_play,
    save_excel,
)

app = Flask(__name__, template_folder='web/templates', static_folder='web/static')
app.config['TEMPLATES_AUTO_RELOAD'] = True

# ==================== 会话管理 ====================

sessions = {}
SESSION_TTL = 1800
MAX_RECORDS = 1000      # 单会话对局记录(局)上限


def _cleanup_sessions():
    now = time.time()
    expired = [sid for sid, s in sessions.items() if now - s._last_access > SESSION_TTL]
    for sid in expired:
        sessions.pop(sid, None)


class WebSession:
    """包装游戏引擎的 Web 会话 — 逐步骤推进"""

    def __init__(self, seed=None):
        self.seed = seed if seed is not None else random.randint(1, 999999)
        random.seed(self.seed)
        self.team_a_level = '2'
        self.team_b_level = '2'
        self.dealer_pid = random.randint(0, 3)
        self.rnd = 0
        self.records = []
        self.winner = None
        self.game_over = False

        self.rec = None
        self.hands = None
        self.bots = {}
        self.trick_leader = 0
        self.current_trick = None
        self.current_call = None            # 当前亮主/反主展示 {action,pid,count,suit}
        self.current_stir = None            # 当前炒底事件 {pid,count,label,taken,discarded}
        self._call_show_idx = 0             # 已展示的叫主条数
        self._stir_show_idx = 0             # 已展示的炒底条数
        self._no_call_shown = False
        self._no_stir_shown = False
        self._stir_computed = False
        self.engine_state = 'idle'
        self.limit_reached = False          # 对局记录数达上限后冻结(需重置才能继续)
        self._last_status = '就绪 | 点击「开始」启动游戏'
        self._last_access = time.time()

        self._timeline = []          # 每步快照 (序列化 dict) 的完整时间线
        self._view = 0               # 当前显示位置 (0..live_steps)
        self._live_steps = 0         # 引擎已执行步数
        self._round_start_step = {}  # rnd -> 该局"初始快照"在时间线中的下标

    # ==================== 发牌 / 开局 ====================

    def init_game(self):
        self.team_a_level = '2'
        self.team_b_level = '2'
        self.dealer_pid = random.randint(0, 3)
        self.rnd = 0
        self.records = []
        self.winner = None
        self.game_over = False
        self.limit_reached = False
        self._start_round()
        self._timeline = [self._get_snapshot()]
        self._view = 0
        self._live_steps = 0
        self._round_start_step = {1: 0}
        return self._serve()

    def _start_round(self):
        self.rnd += 1
        dealer = self.dealer_pid
        dt = [dealer, (dealer + 2) % 4]
        at = [(dealer + 1) % 4, (dealer + 3) % 4]
        level = self.team_a_level if dealer in (0, 2) else self.team_b_level
        self.rec = RoundRecord(self.rnd, dealer, level, dt, at,
                               self.team_a_level, self.team_b_level)
        self.hands = self._deal()
        self.bots = {}
        self.trick_leader = dealer
        self.current_trick = None
        self.current_call = None
        self.current_stir = None
        self._call_show_idx = 0
        self._stir_show_idx = 0
        self._no_call_shown = False
        self._no_stir_shown = False
        self._stir_computed = False
        self.engine_state = 'trump'
        self._set_status(f"第 {self.rnd} 局 | 庄家=玩家{dealer + 1} | 打 {level}")
        self.rec.log(f"=== 第{self.rnd}局 | 庄家=玩家{dealer+1} 打{level} ===")
        self.rec.log(f"庄家方: 玩{dt[0]+1}、玩{dt[1]+1} | 闲家方: 玩{at[0]+1}、玩{at[1]+1}")
        # 结构化事件流: 前端按局/圈排版对齐展示
        self.rec.events = []
        self.rec.events.append({'type': 'round', 'rnd': self.rnd, 'dealer_pid': dealer,
                                'level': level, 'dt': list(dt), 'at': list(at)})

    def _deal(self):
        deck = create_deck()
        random.shuffle(deck)
        hands = [[] for _ in range(4)]
        bottom = []
        for i, card in enumerate(deck):
            (hands[i % 4] if i < 156 else bottom).append(card)
        self.rec.initial_bottom = list(bottom)
        return hands

    # ==================== 阶段 ====================

    def step(self):
        if self._view < self._live_steps:          # 回放中前进: 仅移游标
            self._view += 1
            return self._serve()
        if self.game_over:
            return self._serve()
        # 达 1000 局上限且已结算: 冻结, 不开新局 (仅回放/导出/重置可用)
        if (self.engine_state == 'settled' and not self.game_over
                and len(self.records) >= MAX_RECORDS):
            if not self.limit_reached:
                self.limit_reached = True
                self._set_status(f'⚠️ 已达 {MAX_RECORDS} 局上限 | 可回放/导出，开始新对局前请先「重置对局」')
            self._timeline[self._view] = self._get_snapshot()   # 刷新当前快照(带 limit 标记)
            return self._serve()
        prev_rnd = self.rec.rnd if self.rec else None
        if self.engine_state == 'trump':
            self._do_trump()
        elif self.engine_state == 'bury':
            self._do_bury()
        elif self.engine_state == 'stir':
            self._do_stir()
        elif self.engine_state == 'playing':
            self._play_one_trick()
        elif self.engine_state == 'settled':
            self._next_round()
        self._live_steps += 1
        self._timeline.append(self._get_snapshot())   # 归档一步
        self._view = self._live_steps
        if self.rec and self.rec.rnd != prev_rnd:     # 跨局: 记录新局起点
            self._round_start_step[self.rec.rnd] = self._live_steps
        return self._serve()

    def _do_trump(self):
        """亮主/反主: 一次点击只展示一条叫主。全部展示完后下一次点击进入埋底。"""
        rec = self.rec
        if not rec.call_history:
            ts, calls = determine_trump(self.hands, rec.level, rec.dealer_pid)
            rec.trump_suit = ts
            rec.call_history = calls
            if calls:
                rec.trump_method = 'call'
                rec.call_info = calls[-1]
                parts = ' → '.join(f"玩{c['pid']+1} {c['count']}张{c['suit']}"
                                   for c in calls)
                rec.log(f"【亮主/反主】{parts} → 主花色={SUIT_CN.get(ts, ts)}")
            else:
                rec.trump_method = 'none'
                rec.log("【亮主】无人叫主 → 打无主")
            rec.events.append({'type': 'trump', 'method': rec.trump_method,
                               'suit': ts, 'calls': [dict(c) for c in calls]})
        total = len(rec.call_history)
        if total == 0:
            if not self._no_call_shown:
                self._no_call_shown = True
                self.current_call = None
                self._set_status("⭐ 无人亮主 → 本局打无主")
                return
        elif self._call_show_idx < total:
            c = rec.call_history[self._call_show_idx]
            self._call_show_idx += 1
            self.current_call = c
            self._set_status(f"⭐ {c['action']}: 玩家{c['pid'] + 1} {c['count']}张{c['suit']}")
            return
        # 所有叫主已展示 → 进入埋底
        self._do_bury()

    def _do_bury(self):
        rec = self.rec
        pid = rec.dealer_pid
        hand = list(self.hands[pid]) + list(rec.initial_bottom)
        bot = Bot(pid, hand, 'dealer', rec.level, rec.trump_suit)
        buried = bot.select_for_bottom(6)
        self.hands[pid] = list(bot.hand)
        rec.buried_cards = list(buried)
        rec.bottom = list(buried)
        rec.bury_pid = pid
        bs = sum(SCORE_VALUES.get(c.rank, 0) for c in rec.bottom)
        rec.bottom_score = bs
        self.current_call = None
        rec.log(f"【埋底】庄家埋: {cards_str(buried)} (底分={bs})")
        rec.events.append({'type': 'bury', 'pid': pid,
                           'cards': self._cards_to_dicts(buried), 'score': bs})
        self._set_status(f"📦 埋底: 庄家(玩家{pid + 1}) 埋6张 (底分={bs})")
        self.engine_state = 'stir'

    def _do_stir(self):
        """炒底: 一次点击只展示一条炒底记录。全部展示完后下一次点击开始出牌。"""
        rec = self.rec
        if not self._stir_computed:
            events, final_bottom = determine_stir(self.hands, rec.level, rec.dealer_pid,
                                                  list(rec.bottom), rec.trump_suit)
            rec.stir_events = events
            rec.bottom = final_bottom
            rec.bottom_score = sum(SCORE_VALUES.get(c.rank, 0) for c in final_bottom)
            self._stir_computed = True
            for e in events:
                rec.log(f"【炒底】玩{e['pid']+1} 以{e['label']}炒底: "
                        f"换入 {cards_str(e['taken'])} | 弃出 {cards_str(e['discarded'])}")
            if events:
                rec.log(f"炒底后底牌: {cards_str(final_bottom)} (底分={rec.bottom_score})")
            else:
                rec.log("【炒底】无人炒底")
            rec.events.append({'type': 'stir', 'items': [
                {'pid': e['pid'], 'label': e['label'],
                 'taken': self._cards_to_dicts(e['taken']),
                 'discarded': self._cards_to_dicts(e['discarded'])} for e in events],
                'final_score': rec.bottom_score})
        total = len(rec.stir_events)
        if total == 0:
            if not self._no_stir_shown:
                self._no_stir_shown = True
                self.current_stir = None
                self._set_status("🍳 无人炒底")
                return
        elif self._stir_show_idx < total:
            e = rec.stir_events[self._stir_show_idx]
            self._stir_show_idx += 1
            self.current_stir = e
            self._set_status(f"🍳 炒底: 玩家{e['pid'] + 1} 以{e['label']}炒底, "
                             f"换入 {cards_str(e['taken'])} | 弃出 {cards_str(e['discarded'])}")
            return
        # 所有炒底已展示 → 重建 bots (炒底会换手牌) 并开始出牌
        for p in range(4):
            side = 'dealer' if p in rec.dt else 'attacker'
            self.bots[p] = Bot(p, self.hands[p], side, rec.level, rec.trump_suit)
        self.trick_leader = rec.dealer_pid
        self.current_stir = None
        self.engine_state = 'playing'
        self._play_one_trick()

    def _play_one_trick(self):
        rec = self.rec
        bots = self.bots
        if not any(bots[p].hand for p in range(4)):
            self._settle_round()
            return
        leader = self.trick_leader
        lead_cards = bots[leader].lead([bots[p].hand for p in range(4) if p != leader])
        if not lead_cards:
            self._settle_round()
            return

        best_pid, best_cards = leader, lead_cards
        lead_action = label_play(lead_cards, lead_cards, rec.level, rec.trump_suit,
                                 other_hands=[bots[p].hand for p in range(4) if p != leader],
                                 is_leader=True, beats_best=True)
        best_action = lead_action
        actions = [lead_action]
        played = [(leader, lead_cards)]
        for pos in range(1, 4):
            pid = (leader + pos) % 4
            if not bots[pid].hand:
                played.append((pid, []))
                actions.append(None)
                continue
            cards = bots[pid].follow(lead_cards, best_cards, played)
            played.append((pid, cards))
            beats = bool(cards) and compare_plays(best_cards, cards, rec.level, rec.trump_suit) == 1
            action = label_play(cards, lead_cards, rec.level, rec.trump_suit, beats_best=beats)
            # 盖毙: 第二次及以后的毙牌(出牌权交换)——只要毙掉先前已毙出的牌即标记盖毙
            if (action in ('kill', 'kill_throw')
                    and best_action in ('kill', 'kill_throw', 'over_kill') and beats):
                action = 'over_kill'
            actions.append(action)
            if beats:
                best_pid, best_cards = pid, cards
                best_action = action

        pattern = classify(lead_cards, rec.level, rec.trump_suit)['type']
        winner_side = 'dealer' if best_pid in rec.dt else 'attacker'
        score = sum(SCORE_VALUES.get(c.rank, 0) for _, cl in played for c in cl)
        trick = {'num': len(rec.tricks) + 1, 'leader': leader, 'played': played,
                 'played_actions': actions, 'winner': best_pid, 'winner_side': winner_side,
                 'winner_cards': best_cards, 'pattern': pattern, 'score': score}
        rec.tricks.append(trick)
        parts = []
        for (pid, cl), a in zip(played, actions):
            tag = f"·{ACTION_CN[a]}" if a else ''
            parts.append(f"玩{pid+1}{tag}:{cards_str(cl)}")
        rec.log(f"第{trick['num']}圈[{PATTERN_CN.get(pattern, '?')}]: "
                f"{' | '.join(parts)} → 赢:玩{best_pid+1}({cards_str(best_cards)}) +{score}分")
        rec.events.append({'type': 'trick', 'num': trick['num'],
                           'pattern_cn': PATTERN_CN.get(pattern, ''),
                           'leader': leader,
                           'plays': [{'pid': pid, 'cards': self._cards_to_dicts(cl),
                                      'action_cn': ACTION_CN.get(a, '') if a else ''}
                                     for (pid, cl), a in zip(played, actions)],
                           'winner': best_pid, 'winner_side': winner_side, 'score': score})
        self.current_trick = trick
        self.trick_leader = best_pid
        self._set_status(f"第 {self.rnd} 局 | 第{trick['num']}圈 [{PATTERN_CN.get(pattern, '')}] "
                         f"赢:玩家{best_pid + 1} (+{score}分)")

        if not any(bots[p].hand for p in range(4)):
            self._settle_round()

    def _settle_round(self):
        rec = self.rec
        rec.attacker_score = sum(tr['score'] for tr in rec.tricks
                                 if tr['winner_side'] == 'attacker')
        settle_round(rec, self)
        rec.log(f"结算: 闲家得分={rec.attacker_score} "
                f"庄方+{rec.dealer_up} 闲方+{rec.attacker_up} | {rec.result}")
        rec.events.append({'type': 'settle', 'attacker_score': rec.attacker_score,
                           'dealer_up': rec.dealer_up, 'attacker_up': rec.attacker_up,
                           'result': rec.result, 'side_switch': rec.side_switch,
                           'koudi': rec.koudi, 'bottom_score': rec.bottom_score,
                           'koudi_multiplier': rec.koudi_multiplier,
                           'koudi_pattern_cn': (PATTERN_CN.get(rec.tricks[-1]['pattern'], '')
                                                if rec.koudi and rec.tricks else '')})
        self.records.append(rec)
        self.engine_state = 'settled'
        self._set_status(f"📊 结算 | 闲家得分={rec.attacker_score} | {rec.result}")

    def _next_round(self):
        if self.game_over:
            self._set_status(f"🏆 游戏结束 | 胜方: {self.winner or '—'}")
            return
        self._start_round()

    # ==================== 回放 (快照时间线 + 视图游标) ====================

    def _serve(self):
        """返回当前 view 的快照, 并覆盖游标字段 (历史快照里的 view/live_steps 是旧值)"""
        snap = dict(self._timeline[self._view])
        snap['view'] = self._view
        snap['live_steps'] = self._live_steps
        snap['replay'] = self._view < self._live_steps    # 是否回放态
        snap['can_prev'] = self._view > 0
        return snap

    def prev_step(self):
        """上一步: 只移动游标, 引擎状态不动"""
        if self._view > 0:
            self._view -= 1
        return self._serve()

    def replay_round(self, rnd):
        """回放某一局: 跳到该局初始快照"""
        start = self._round_start_step.get(rnd)
        if start is None:
            return None
        self._view = start
        return self._serve()

    def live(self):
        """退出回放: 回到直播前沿 (只移动游标, 引擎状态不动)"""
        self._view = self._live_steps
        return self._serve()

    # ==================== 序列化 ====================

    def _card_to_dict(self, card):
        if card is None:
            return None
        return {'suit': card.suit, 'rank': card.rank}

    def _cards_to_dicts(self, cards):
        if not cards:
            return []
        return [self._card_to_dict(c) for c in cards]

    def _sort_hand(self, cards):
        trump = self.rec.trump_suit if self.rec else None
        level = self.rec.level if self.rec else '2'
        return sorted(cards, key=lambda c: cp(c, level, trump), reverse=True)

    def _get_snapshot(self):
        rec = self.rec
        hands_data = {}
        for pid in range(4):
            if self.engine_state in ('playing', 'settled') and pid in self.bots:
                source = self.bots[pid].hand
            elif self.hands and pid < len(self.hands):
                source = self.hands[pid]
            else:
                source = []
            hands_data[str(pid)] = self._cards_to_dicts(self._sort_hand(source))

        # 底牌: 炒底逐步展示时显示"当前炒底之后的底牌", 否则显示最终底牌
        if self.engine_state == 'stir' and self.current_stir and 'bottom_after' in self.current_stir:
            bottom_disp = self.current_stir['bottom_after']
        else:
            bottom_disp = rec.bottom if rec else []
        data = {
            'state': self.engine_state,
            'round': self.rnd,
            'game_over': self.game_over,
            'seed': self.seed,
            'dealer_pid': self.dealer_pid,
            'team_a_level': self.team_a_level,
            'team_b_level': self.team_b_level,
            'hands': hands_data,
            'bottom': self._cards_to_dicts(bottom_disp),
            'initial_bottom': self._cards_to_dicts(rec.initial_bottom) if rec else [],
            'status': self._last_status,
            'winner': self.winner,
            'records_count': len(self.records),
            'limit_reached': self.limit_reached,
        }

        if self.current_call:
            c = self.current_call
            data['current_call'] = {'action': c['action'], 'pid': c['pid'],
                                    'count': c['count'], 'suit': c['suit']}
        if self.current_stir:
            cs = self.current_stir
            data['current_stir'] = {
                'pid': cs['pid'], 'count': cs['count'], 'label': cs['label'],
                'qualify': self._cards_to_dicts(cs['qualify']),
                'taken': self._cards_to_dicts(cs['taken']),
                'discarded': self._cards_to_dicts(cs['discarded']),
            }
        data['stir_revealed'] = self._stir_show_idx

        if rec:
            data['round_record'] = {
                'rnd': rec.rnd,
                'level': rec.level,
                'dt': rec.dt,
                'at': rec.at,
                'dealer_pid': rec.dealer_pid,
                'trump_suit': rec.trump_suit,
                'trump_suit_cn': SUIT_CN.get(rec.trump_suit, '') if rec.trump_suit else '',
                'trump_method': rec.trump_method,
                'call_info': rec.call_info,
                'call_history': [{'action': c['action'], 'pid': c['pid'],
                                  'count': c['count'], 'suit': c['suit']}
                                 for c in rec.call_history],
                'stir_events': [{'pid': e['pid'], 'count': e['count'], 'label': e['label'],
                                 'taken': self._cards_to_dicts(e['taken']),
                                 'discarded': self._cards_to_dicts(e['discarded'])}
                                for e in rec.stir_events],
                'bury_pid': rec.bury_pid,
                'buried_cards': self._cards_to_dicts(rec.buried_cards),
                'tricks_count': len(rec.tricks),
                'attacker_score': rec.attacker_score,
                'bottom_score': rec.bottom_score,
                'koudi': rec.koudi,
                'koudi_multiplier': rec.koudi_multiplier,
                'koudi_pattern': (PATTERN_CN.get(rec.tricks[-1]['pattern'], '')
                                  if rec.koudi and rec.tricks else ''),
                'dealer_up': rec.dealer_up,
                'attacker_up': rec.attacker_up,
                'result': rec.result,
                'side_switch': rec.side_switch,
                'team_a_level_after': rec.team_a_level_after,
                'team_b_level_after': rec.team_b_level_after,
            }

        if self.current_trick:
            t = self.current_trick
            pa = t.get('played_actions', [None] * len(t['played']))
            data['current_trick'] = {
                'num': t['num'],
                'leader': t['leader'],
                'pattern': t['pattern'],
                'pattern_cn': PATTERN_CN.get(t['pattern'], ''),
                'winner': t['winner'],
                'winner_side': t['winner_side'],
                'score': t['score'],
                'played': [
                    {'pid': pid, 'cards': self._cards_to_dicts(cl),
                     'action': a, 'action_cn': ACTION_CN.get(a, '') if a else ''}
                    for (pid, cl), a in zip(t['played'], pa)
                ],
            }

        return data

    def _set_status(self, text):
        self._last_status = text


# ==================== API 路由 ====================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/new', methods=['POST'])
def api_new():
    data = request.json or {}
    seed = data.get('seed')
    sid = f"game_{int(time.time() * 1000)}_{len(sessions)}"
    sess = WebSession(seed=seed)
    sessions[sid] = sess
    snapshot = sess.init_game()
    return jsonify({'session_id': sid, **snapshot})


@app.route('/api/step', methods=['POST'])
def api_step():
    sid = request.json.get('session_id')
    _cleanup_sessions()
    sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    sess._last_access = time.time()
    return jsonify(sess.step())


@app.route('/api/status', methods=['GET'])
def api_status():
    sid = request.args.get('session_id')
    _cleanup_sessions()
    sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    sess._last_access = time.time()
    return jsonify(sess._serve())


@app.route('/api/prev', methods=['POST'])
def api_prev():
    sid = request.json.get('session_id')
    _cleanup_sessions()
    sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    sess._last_access = time.time()
    return jsonify(sess.prev_step())


@app.route('/api/replay', methods=['POST'])
def api_replay():
    data = request.json or {}
    sid = data.get('session_id')
    rnd = data.get('rnd')
    _cleanup_sessions()
    sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    sess._last_access = time.time()
    snap = sess.replay_round(rnd)
    if snap is None:
        return jsonify({'error': 'round not found'}), 404
    return jsonify(snap)


@app.route('/api/live', methods=['POST'])
def api_live():
    sid = request.json.get('session_id')
    _cleanup_sessions()
    sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    sess._last_access = time.time()
    return jsonify(sess.live())


def _game_row(sess, rec, in_progress):
    """对局记录一行: 级牌/获胜方/得分数/升级数 (获胜方由结算结果推导)"""
    result = rec.result
    if result.startswith('队伍A'):
        winner = '队伍A'
    elif result.startswith('队伍B'):
        winner = '队伍B'
    elif result.startswith('庄家'):
        winner = '庄家方'
    else:
        winner = '闲家方'
    return {
        'rnd': rec.rnd,
        'level': rec.level,                      # 本局级牌
        'winner': '进行中' if in_progress else winner,
        'score': None if in_progress else rec.attacker_score,
        'dealer_up': rec.dealer_up,
        'attacker_up': rec.attacker_up,
        'start_step': sess._round_start_step.get(rec.rnd),
        'result': rec.result,
        'in_progress': in_progress,
    }


@app.route('/api/games', methods=['GET'])
def api_games():
    sid = request.args.get('session_id')
    _cleanup_sessions()
    sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    sess._last_access = time.time()
    games = [_game_row(sess, rec, False) for rec in sess.records]
    if sess.rec and sess.rec.rnd > len(sess.records):
        games.append(_game_row(sess, sess.rec, True))
    return jsonify({'games': games})


@app.route('/api/logs', methods=['GET'])
def api_logs():
    sid = request.args.get('session_id')
    rnd = request.args.get('rnd', type=int)
    _cleanup_sessions()
    sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    sess._last_access = time.time()
    if sess.rec and rnd == sess.rec.rnd:
        rec = sess.rec
    elif 1 <= (rnd or 0) <= len(sess.records):
        rec = sess.records[rnd - 1]
    else:
        return jsonify({'error': 'round not found'}), 404
    return jsonify({'rnd': rnd, 'events': getattr(rec, 'events', []),
                    'logs': rec.logs})


@app.route('/api/reset', methods=['POST'])
def api_reset():
    sid = request.json.get('session_id')
    if sid in sessions:
        del sessions[sid]
    return jsonify({'status': 'ok'})


@app.route('/api/export', methods=['POST'])
def api_export():
    """导出全部对局记录为 Excel(.xlsx)"""
    sid = request.json.get('session_id')
    _cleanup_sessions()
    sess = sessions.get(sid)
    if not sess:
        return jsonify({'error': 'session not found'}), 404
    sess._last_access = time.time()

    class _FakeGame:
        pass

    g = _FakeGame()
    g.winner = sess.winner
    g.seed = sess.seed
    buf = io.BytesIO()
    save_excel(sess.records, g, buf)
    buf.seek(0)
    fname = f"三副牌升级_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(buf,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=fname)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-debug', action='store_true')
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()
    print("== 三副牌升级 Web v1.2 ==")
    print(f"  -> http://localhost:{args.port}")
    app.run(host='0.0.0.0', port=args.port, debug=not args.no_debug)
