#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三副牌升级 · 游戏引擎 v1.0

三副牌(162张), 4人游戏, 每人39张, 底牌6张。
对局流程: 发牌 → 亮主/反主(无人叫主则打无主) → 庄家埋底(底分≤80) → 闲家炒底 → 出牌 → 结算升级(过A获胜)

规则来源: 2026.8.31-三副牌升级.md / 三副牌升级游戏规则.md

v1.0 说明:
  - 全自动对局(4个AI), 配合 Web 端逐步/自动播放
  - 已实现: 亮主/反主(级牌叫主, 张数多的反张数少的, 3反2/2反1, 同数量先叫为大, 反主须换花色)、
    庄家埋底(底分≤80拦截)、闲家炒底(2张炒1张/3张炒2张, 炒牌仅作资格, 手牌39/底牌6不变)、
    2常主、单张/对子/拖拉机/刻子/推土机、主牌杀、垫牌、分牌计分、抠底加倍、升级表
  - 暂缓(后续版本): 甩牌、主2/副2 及 主2/副级牌 的特殊拖拉机衔接按链序实现
"""

import random
from collections import defaultdict

# ==================== 常量 ====================

SUITS = ['♠', '♥', '♣', '♦']
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
RANK_ORDER = {r: i for i, r in enumerate(RANKS)}          # 2最小 ... A最大
SCORE_RANKS = {'5', '10', 'K'}
SCORE_VALUES = {'5': 5, '10': 10, 'K': 10}
SUIT_CN = {'♠': '黑桃', '♥': '红桃', '♣': '草花', '♦': '方块'}
TRUMP_RANKS_DESC = ['A', 'K', 'Q', 'J', '10', '9', '8', '7', '6', '5', '4', '3']

# 等级循环: 从2打到A, 过A获胜
LEVEL_CYCLE = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
LEVEL_LEN = len(LEVEL_CYCLE)

PATTERN_CN = {'single': '单张', 'pair': '对子', 'triplet': '刻子',
              'tractor': '拖拉机', 'bulldozer': '推土机', 'throw': '甩牌'}

# 出牌操作名 (显示在出牌牌组下方)
ACTION_CN = {'kill': '毙牌', 'kill_throw': '毙甩牌', 'over_kill': '盖毙',
             'throw': '甩牌', 'throw_fail': '甩牌失败'}


def cards_str(cards):
    return ' '.join(str(c) for c in cards) if cards else '无'


# ==================== 牌类 ====================

class Card:
    def __init__(self, suit, rank):
        self.suit = suit
        self.rank = rank

    def __repr__(self):
        if self.rank in ('大王', '小王'):
            return self.rank
        return f"{self.suit}{self.rank}"

    def __eq__(self, other):
        return isinstance(other, Card) and self.suit == other.suit and self.rank == other.rank

    def __hash__(self):
        return hash((self.suit, self.rank))


def create_deck():
    """三副牌: 162张 (每花色每点数×3 + 大王×3 + 小王×3)"""
    deck = []
    for _ in range(3):
        for suit in SUITS:
            for rank in RANKS:
                deck.append(Card(suit, rank))
        deck.append(Card('王', '大王'))
        deck.append(Card('王', '小王'))
    return deck


# ==================== 牌力判定 ====================

def is_main(card, level, trump_suit):
    """主牌: 大王/小王/级牌/2常主/主花色"""
    if card.rank in ('大王', '小王'):
        return True
    if card.rank == level:            # 级牌(全花色)
        return True
    if card.rank == '2':              # 2常主(全花色)
        return True
    if trump_suit and card.suit == trump_suit:
        return True
    return False


def main_chain_rank(card, level, trump_suit):
    """主牌拖拉机链序号(越小越强): 大王0 小王1 级牌2 常主2=3 主花色A=4 K=5 ..."""
    if card.rank == '大王':
        return 0
    if card.rank == '小王':
        return 1
    if card.rank == level:
        return 2
    if card.rank == '2':
        return 3
    if card.suit == trump_suit:
        return 4 + (13 - RANK_ORDER[card.rank])
    return -1


def chain_idx(card, level, trump_suit):
    """组合排序用链序: 主牌用主链, 副牌用点数序"""
    if is_main(card, level, trump_suit):
        return main_chain_rank(card, level, trump_suit)
    return RANK_ORDER[card.rank]


def card_power(card, level, trump_suit):
    """(组, 值): 6大王 5小王 4级牌 3常主2 2主花色 1副牌"""
    if card.rank == '大王':
        return (6, 100)
    if card.rank == '小王':
        return (5, 100)
    if card.rank == level:
        return (4, RANK_ORDER[card.rank])
    if card.rank == '2':
        return (3, 100)
    if card.suit == trump_suit:
        return (2, RANK_ORDER[card.rank])
    return (1, RANK_ORDER[card.rank])


def cp(card, level, trump_suit):
    g, v = card_power(card, level, trump_suit)
    return g * 1000 + v


# ==================== 牌型 ====================

def _adjacent(a, b):
    """链序相邻判定 (d==0 仅允许级牌块/2块内部并列)"""
    d = b - a
    if d == 0:
        return a in (2, 3)
    return d == 1


def _consecutive(idxs):
    idxs = sorted(idxs)
    prev = idxs[0]
    for cur in idxs[1:]:
        if not _adjacent(prev, cur):
            return False
        prev = cur
    return True


def classify(cards, level, trump_suit):
    """识别一组出牌的牌型。
    返回: {type, all_main, suit, groups, top}
      type: single/pair/triplet/tractor/bulldozer/throw
    """
    if not cards:
        return None
    all_main = all(is_main(c, level, trump_suit) for c in cards)
    if not all_main:
        suits = set(c.suit for c in cards)
        if len(suits) != 1:
            # 异花色垫牌 → 甩牌
            return {'type': 'throw', 'all_main': False, 'suit': cards[0].suit,
                    'groups': {}, 'top': None}

    g = defaultdict(list)
    for c in cards:
        g[(c.suit, c.rank)].append(c)

    info = {'type': 'throw', 'all_main': all_main,
            'suit': None if all_main else cards[0].suit,
            'groups': dict(g),
            'top': max(cards, key=lambda c: cp(c, level, trump_suit))}

    keys = sorted(g.keys(), key=lambda k: chain_idx(g[k][0], level, trump_suit))
    counts = [len(g[k]) for k in keys]
    idxs = [chain_idx(g[k][0], level, trump_suit) for k in keys]

    if len(keys) == 1:
        cnt = counts[0]
        if cnt == 1:
            info['type'] = 'single'
        elif cnt == 2:
            info['type'] = 'pair'
        elif cnt == 3:
            info['type'] = 'triplet'
        return info

    if all(c == counts[0] for c in counts) and _consecutive(idxs):
        if counts[0] == 2:
            info['type'] = 'tractor'
        elif counts[0] == 3:
            info['type'] = 'bulldozer'
    return info


def _throw_has_bigger(cards, other_hands, level, trump_suit):
    """甩牌校验: 甩出的单花色副牌中, 是否存在其他玩家手中的同花色更大牌 → 甩牌失败"""
    suit = cards[0].suit
    others = [c for h in other_hands for c in h
              if not is_main(c, level, trump_suit) and c.suit == suit]
    for c in cards:
        if any(RANK_ORDER.get(o.rank, 0) > RANK_ORDER.get(c.rank, 0) for o in others):
            return True
    return False


def label_play(cards, lead_cards, level, trump_suit, other_hands=None,
               is_leader=False, beats_best=False):
    """识别一次出牌的操作名, 返回 ACTION_CN 的键; 无特殊操作返回 None。
      kill       毙牌     — 以主牌毙副牌领出且该牌实际赢得本圈(出牌权交换)
      kill_throw 毙甩牌   — 以主牌毙甩牌 (结构对应且赢得本圈)
      throw      甩牌     — 首家单花色副牌多牌型一并打出 (该花色均为当前最大)
      throw_fail 甩牌失败 — 甩出牌中存在他手更大的同花色牌 → 强制出最小牌
      (over_kill 盖毙 = 第二次及以后的毙牌/毙甩牌, 出牌权交换; 由调用方结合先前是否已毙出判定)
      beats_best: 本次出牌是否压过当前最大牌(赢得出牌权)。垫主牌不能算毙牌, 必须实际赢下才标毙。
    """
    if not cards:
        return None
    lead_main = bool(lead_cards) and all(is_main(c, level, trump_suit) for c in lead_cards)
    ci = classify(cards, level, trump_suit)
    play_main = ci['all_main'] if ci else False

    # 以主牌毙牌 (跟随副牌领出, 且实际赢得本圈)
    if lead_cards and not lead_main and play_main and beats_best:
        if classify(lead_cards, level, trump_suit)['type'] == 'throw':
            return 'kill_throw'          # 毙甩牌
        return 'kill'                    # 毙牌

    # 甩牌 (仅限首家: 单花色副牌多牌型组合)
    if (is_leader and ci and ci['type'] == 'throw' and not play_main
            and len({c.suit for c in cards}) == 1):
        if other_hands and _throw_has_bigger(cards, other_hands, level, trump_suit):
            return 'throw_fail'
        return 'throw'
    return None


def compare_plays(A, B, level, trump_suit):
    """比较两组出牌: 1=B大, -1=A大, 0=平(先出大)
    A 为当前最大(先出), B 为后出者。
    """
    ia = classify(A, level, trump_suit)
    ib = classify(B, level, trump_suit)
    if ia is None:
        return 0
    if ib is None:
        return -1
    # 牌型结构不同 → B 未匹配 → A 赢
    if ia['type'] != ib['type']:
        return -1
    # 类别判定
    if ia['all_main']:
        if not ib['all_main']:
            return -1            # B 垫副牌
    else:
        if not ib['all_main'] and ib['suit'] != ia['suit']:
            return -1            # B 异花色垫牌
    pa = max(cp(c, level, trump_suit) for c in A)
    pb = max(cp(c, level, trump_suit) for c in B)
    if pb > pa:
        return 1
    if pb < pa:
        return -1
    return 0


def koudi_multiplier(cards, level, trump_suit):
    """抠底倍数: 单张2 对子4 刻子8 拖拉机/推土机 2的n次方(n=张数)"""
    ci = classify(cards, level, trump_suit)
    t = ci['type'] if ci else 'single'
    if t == 'single':
        return 2
    if t == 'pair':
        return 4
    if t == 'triplet':
        return 8
    if t in ('tractor', 'bulldozer'):
        return 2 ** len(cards)
    # 甩牌按最高结构(简化: 张数越多越大)
    n = len(cards)
    return 2 ** n if n >= 4 else (4 if n == 2 else 2)


# ==================== 机器人 ====================

class Bot:
    def __init__(self, pid, hand, side, level, trump_suit):
        self.pid = pid
        self.hand = list(hand)
        self.side = side            # 'dealer' 庄家方 / 'attacker' 闲家方
        self.level = level
        self.ts = trump_suit

    # ---------- 辅助 ----------
    def _main(self):
        return [c for c in self.hand if is_main(c, self.level, self.ts)]

    def _off(self):
        return [c for c in self.hand if not is_main(c, self.level, self.ts)]

    def _suits_off(self):
        d = defaultdict(list)
        for c in self._off():
            d[c.suit].append(c)
        return d

    def _play(self, cards):
        for c in cards:
            if c in self.hand:
                self.hand.remove(c)

    def _combos_of(self, pool):
        """pool 为同类别牌(全主 或 同一花色副牌)。返回各牌型最小组合。"""
        res = {'single': [], 'pair': [], 'triplet': [], 'tractor': [], 'bulldozer': []}
        g = defaultdict(list)
        for c in pool:
            g[(c.suit, c.rank)].append(c)
        items = sorted(g.items(), key=lambda kv: chain_idx(kv[1][0], self.level, self.ts))

        for k, v in items:
            res['single'].append([v[0]])
        for k, v in items:
            if len(v) >= 2:
                res['pair'].append(v[:2])
            if len(v) >= 3:
                res['triplet'].append(v[:3])

        def _runs(min_cnt):
            runs, run = [], []
            for k, v in items:
                if len(v) >= min_cnt:
                    if run:
                        ia = chain_idx(run[-1][1][0], self.level, self.ts)
                        ib = chain_idx(v[0], self.level, self.ts)
                        if not _adjacent(ia, ib):
                            runs.append(run)
                            run = []
                    run.append((k, v))
                else:
                    if run:
                        runs.append(run)
                        run = []
            if run:
                runs.append(run)
            return runs

        for r in _runs(2):
            if len(r) >= 2:
                cards = []
                for k, v in r:
                    cards.extend(v[:2])
                res['tractor'].append(cards)
        for r in _runs(3):
            if len(r) >= 2:
                cards = []
                for k, v in r:
                    cards.extend(v[:3])
                res['bulldozer'].append(cards)
        return res

    # ---------- 首出 ----------
    def lead(self):
        """首出策略: 有大牌型(推土机/拖拉机/刻子)优先出大的;
        同牌型内无分牌优先, 再选强的/张数多的。"""
        if not self.hand:
            return []
        TYPE_PRI = {'bulldozer': 5, 'tractor': 4, 'triplet': 3, 'pair': 2, 'single': 1}

        def _strongest(pool):
            """返回 pool 中优先牌型里最强的一组 (type, cards)"""
            combos = self._combos_of(pool)
            for t in ('bulldozer', 'tractor', 'triplet', 'pair', 'single'):
                if combos[t]:
                    c = max(combos[t], key=lambda x: max(cp(k, self.level, self.ts) for k in x))
                    return t, c
            return None, None

        candidates = []                        # (牌型优先级, 组合)
        for suit, cards in self._suits_off().items():
            t, c = _strongest(cards)
            if c:
                candidates.append((TYPE_PRI[t], c))
        if candidates:
            candidates.sort(key=lambda tc: (
                -tc[0],                        # 大牌型优先(推土机>拖拉机>刻子>对子>单张)
                int(any(k.rank in SCORE_RANKS for k in tc[1])),  # 同牌型无分优先
                -max(cp(k, self.level, self.ts) for k in tc[1]),
                -len(tc[1]),
            ))
            pick = candidates[0][1]
            self._play(pick)
            return pick
        # 全主牌 → 同样大牌型优先
        t, c = _strongest(self._main())
        if c:
            self._play(c)
            return c
        return []

    # ---------- 跟牌 ----------
    def follow(self, lead_cards, best_cards, played_so_far):
        need = len(lead_cards)
        if not self.hand:
            return []
        if len(self.hand) <= need:
            out = list(self.hand)
            self.hand.clear()
            return out

        lead_is_main = all(is_main(c, self.level, self.ts) for c in lead_cards)
        lead_suit = None if lead_is_main else lead_cards[0].suit
        ltype = classify(lead_cards, self.level, self.ts)['type']
        has_points = any(c.rank in SCORE_RANKS for _, cl in played_so_far for c in cl)

        if lead_is_main:
            pool = self._main()
        else:
            pool = [c for c in self._off() if c.suit == lead_suit]

        if pool:
            matched = self._follow_same(pool, ltype, need, has_points, best_cards)
            if matched:
                if len(matched) < need:
                    return self._fill_from_hand(matched, need)
                return matched

        # 无同类别可跟 → 主牌杀
        if not lead_is_main:
            kill = self._try_kill(ltype, need, best_cards, played_so_far)
            if kill:
                return kill

        # 垫牌
        return self._shed(need)

    def _follow_same(self, pool, ltype, need, has_points, best_cards):
        combos = self._combos_of(pool)
        if ltype in ('tractor', 'bulldozer'):
            cands = [c for c in combos[ltype] if len(c) == need]
        else:
            cands = combos[ltype]

        if cands:
            if has_points and best_cards:
                wins = [c for c in cands if compare_plays(best_cards, c, self.level, self.ts) == 1]
                if wins:
                    pick = min(wins, key=lambda c: max(cp(x, self.level, self.ts) for x in c))
                    self._play(pick)
                    return pick
            pick = min(cands, key=lambda c: max(cp(x, self.level, self.ts) for x in c))
            self._play(pick)
            return pick

        # 降级补数量
        if ltype == 'tractor':
            pick = []
            for p in combos['pair']:
                pick.extend(p)
                if len(pick) >= need:
                    break
            if len(pick) < need:
                for s in combos['single']:
                    if s[0] not in pick:
                        pick.append(s[0])
                        if len(pick) >= need:
                            break
            pick = self._fill(pick[:need], pool, need)
            self._play(pick)
            return pick
        if ltype == 'triplet':
            pick = []
            if combos['triplet']:
                pick.extend(combos['triplet'][0])
            if len(pick) < 3:
                for p in combos['pair']:
                    if not any(x in pick for x in p):
                        pick.extend(p)
                        break
            if len(pick) < 3:
                for s in combos['single']:
                    if s[0] not in pick:
                        pick.append(s[0])
                        if len(pick) >= 3:
                            break
            pick = self._fill(pick[:need], pool, need)
            self._play(pick)
            return pick
        if ltype == 'pair':
            s = sorted(pool, key=lambda c: (int(c.rank in SCORE_RANKS), chain_idx(c, self.level, self.ts)))
            pick = s[:2]
            self._play(pick)
            return pick
        if ltype == 'single':
            s = sorted(pool, key=lambda c: (int(c.rank in SCORE_RANKS), chain_idx(c, self.level, self.ts)))
            pick = s[:1]
            self._play(pick)
            return pick
        return None

    def _try_kill(self, ltype, need, best_cards, played_so_far):
        pool = self._main()
        if not pool:
            return None
        combos = self._combos_of(pool)
        if ltype in ('tractor', 'bulldozer'):
            cands = [c for c in combos[ltype] if len(c) == need]
        else:
            cands = combos[ltype]
        if not cands:
            return None
        has_points = any(c.rank in SCORE_RANKS for _, cl in played_so_far for c in cl)
        if not has_points:
            return None                       # 无分不浪费主牌
        wins = [c for c in cands if compare_plays(best_cards, c, self.level, self.ts) == 1]
        if not wins:
            return None
        pick = min(wins, key=lambda c: max(cp(x, self.level, self.ts) for x in c))
        self._play(pick)
        return pick

    def _shed(self, need):
        pool = self._off()
        pool.sort(key=lambda c: (int(c.rank in SCORE_RANKS), chain_idx(c, self.level, self.ts)))
        pick = []
        for c in pool:
            if len(pick) >= need:
                break
            pick.append(c)
        if len(pick) < need:
            m = sorted(self._main(), key=lambda c: cp(c, self.level, self.ts))
            for c in m:
                if len(pick) >= need:
                    break
                pick.append(c)
        self._play(pick)
        return pick

    def _fill(self, pick, pool, need):
        for c in pool:
            if len(pick) >= need:
                break
            if c not in pick:
                pick.append(c)
        return pick

    def _fill_from_hand(self, pick, need):
        have = set(pick)
        pool = [c for c in self.hand if c not in have]
        pool.sort(key=lambda c: (int(c.rank in SCORE_RANKS), cp(c, self.level, self.ts)))
        for c in pool:
            if len(pick) >= need:
                break
            pick.append(c)
        self._play(pick)
        return pick

    # ---------- 埋底 ----------
    def select_for_bottom(self, count, max_score=80, aggressive=None):
        """选牌埋底: 优先非主非分小牌, 尽量压低底牌分值; 底分 > max_score 时系统拦截,
        用池中更弱非分牌替换分值牌, 直至底分 ≤ max_score。
        aggressive=None 时随机决定: ~35% 敢埋分(按牌力弱→强选, 可含低价分牌5/10/K, 底分仍≤max),
        否则保守全埋非分牌。"""
        if aggressive is None:
            aggressive = random.random() < 0.35
        if aggressive:
            def pri(c):
                return (1 if is_main(c, self.level, self.ts) else 0,
                        RANK_ORDER.get(c.rank, 0))
        else:
            def pri(c):
                return (1 if is_main(c, self.level, self.ts) else 0,
                        int(c.rank in SCORE_RANKS),
                        RANK_ORDER.get(c.rank, 0))
        s = sorted(self.hand, key=pri)
        sel = list(s[:count])
        pool = list(s[count:])
        bs = sum(SCORE_VALUES.get(c.rank, 0) for c in sel)
        while bs > max_score:
            score_i = next((i for i, c in enumerate(sel) if c.rank in SCORE_RANKS), None)
            if score_i is None or not pool:
                break
            cand = next((c for c in pool if c.rank not in SCORE_RANKS), None)
            if cand is None:
                cand = pool[0]                       # 只能换分值牌
            pool.remove(cand)
            bs = bs - SCORE_VALUES.get(sel[score_i].rank, 0) + SCORE_VALUES.get(cand.rank, 0)
            sel[score_i] = cand
        self._play(sel)
        return sel


# ==================== 叫主 ====================

def determine_trump(hands, level, dealer_pid):
    """亮主/反主: 用级牌叫主, 张数多的反张数少的(3反2, 2反1), 同数量先叫为大。
    反主须用不同花色。庄家先叫, 逆时针轮流。无人叫主 → 打无主(trump_suit=None)。
    返回 (trump_suit, calls): calls 为亮主/反主完整序列 [{action,pid,count,suit}, ...]
    """
    order = [dealer_pid, (dealer_pid + 1) % 4, (dealer_pid + 2) % 4, (dealer_pid + 3) % 4]
    calls = []
    current = None            # (count, suit)
    for pid in order:
        best_suit, best_count = None, 0
        for suit in SUITS:
            n = sum(1 for c in hands[pid] if c.rank == level and c.suit == suit)
            if n > best_count:
                best_suit, best_count = suit, n
        if best_suit and best_count >= 1:
            if current is None:
                current = (best_count, best_suit)
                calls.append({'action': '亮主', 'pid': pid, 'count': best_count, 'suit': best_suit})
            elif best_count > current[0] and best_suit != current[1]:
                current = (best_count, best_suit)
                calls.append({'action': '反主', 'pid': pid, 'count': best_count, 'suit': best_suit})
    if current is None:
        return None, calls
    return current[1], calls


# ==================== 炒底 ====================

STIR_KIND_RANK = {'大王': 6, '小王': 5, '♠': 4, '♥': 3, '♣': 2, '♦': 1}


def stir_combos(hand, level):
    """手牌中可用的炒底组合: [(priority, cards, label), ...] 按 priority 降序。
    炒牌 = 大王×2/3、小王×2/3、同花色级牌×2/3。
    priority = 张数*10 + 种类rank → 3大王(36) > 3小王(35) > 3♠(34) ... > 2♦(21)。
    """
    combos = []
    for jrank in ('大王', '小王'):
        cards = [c for c in hand if c.rank == jrank]
        if len(cards) >= 3:
            combos.append((3 * 10 + STIR_KIND_RANK[jrank], cards[:3], f"3{jrank}"))
        if len(cards) >= 2:
            combos.append((2 * 10 + STIR_KIND_RANK[jrank], cards[:2], f"2{jrank}"))
    for suit in SUITS:
        cards = [c for c in hand if c.rank == level and c.suit == suit]
        if len(cards) >= 3:
            combos.append((3 * 10 + STIR_KIND_RANK[suit], cards[:3], f"3张{SUIT_CN[suit]}{level}"))
        if len(cards) >= 2:
            combos.append((2 * 10 + STIR_KIND_RANK[suit], cards[:2], f"2张{SUIT_CN[suit]}{level}"))
    combos.sort(key=lambda x: x[0], reverse=True)
    return combos


def determine_stir(hands, level, dealer_pid, bottom, trump_suit):
    """炒底: 埋底后、出牌前, 仅闲家可炒, 按逆时针轮流。
    炒牌仅作资格(保留手中): 2张炒牌换入底牌1张、3张换入2张 —— 从底牌换入 N=张数-1 张,
    再从手牌弃出 N 张放回底牌, 手牌保持39张、底牌保持6张。
    后炒者须数量更高或同数量优先级更高才能取代当前炒底。
    AI 决策: 能炒就炒(持有合格炒牌即炒, 不看底牌是否有分)。
    返回 (events, final_bottom)。
    events: [{pid, count, label, taken, discarded}]
    """
    events = []
    bottom_list = list(bottom)
    current_prio = -1
    for pid in ((dealer_pid + 1) % 4, (dealer_pid + 3) % 4):   # 逆时针: 两个闲家
        combos = stir_combos(hands[pid], level)
        if not combos:
            continue
        prio, combo, label = combos[0]                         # 自己最强组合
        if prio <= current_prio:
            continue                                           # 压不过当前炒底
        n = len(combo) - 1                                     # 2张炒1张 / 3张炒2张
        # 从底牌换入最好的 n 张 (分牌优先)
        bottom_list.sort(key=lambda c: (int(c.rank not in SCORE_RANKS),
                                        -cp(c, level, trump_suit)))
        taken = bottom_list[:n]
        rest = bottom_list[n:]
        # 从手牌弃出最弱 n 张放回底牌 (按对象身份移除, 避免误删同值重牌)
        hand = sorted(hands[pid], key=lambda c: (int(c.rank in SCORE_RANKS),
                                                 cp(c, level, trump_suit)))
        discarded = hand[:n]
        discard_ids = {id(c) for c in discarded}
        hands[pid] = [c for c in hands[pid] if id(c) not in discard_ids] + taken
        bottom_list = rest + discarded
        current_prio = prio
        events.append({'pid': pid, 'count': len(combo), 'label': label,
                       'qualify': list(combo),          # 获取炒底资格的炒牌(亮出来)
                       'taken': taken, 'discarded': discarded,
                       'bottom_after': list(bottom_list)})
    return events, bottom_list


# ==================== 等级 ====================

def level_up(lvl, steps):
    """升级, 返回 (新等级, 是否过A)"""
    if steps <= 0:
        return lvl, False
    idx = LEVEL_CYCLE.index(lvl) + steps
    if idx >= LEVEL_LEN:
        return LEVEL_CYCLE[-1], True
    return LEVEL_CYCLE[idx], False


# ==================== 游戏记录 ====================

class RoundRecord:
    def __init__(self, rnd, dealer_pid, level, dt, at, team_a_level, team_b_level):
        self.rnd = rnd
        self.dealer_pid = dealer_pid
        self.level = level                      # 本局级牌(庄家方等级)
        self.dt = dt                            # 庄家方 [p1, p3]
        self.at = at                            # 闲家方 [p2, p4]
        self.team_a_level_before = team_a_level
        self.team_b_level_before = team_b_level
        self.team_a_level_after = team_a_level
        self.team_b_level_after = team_b_level

        self.trump_suit = None
        self.trump_method = 'none'              # 'call' 叫主 / 'none' 无主
        self.call_info = None
        self.call_history = []                  # 亮主/反主完整序列
        self.initial_bottom = []
        self.bury_pid = None
        self.buried_cards = []
        self.bottom = []                        # 埋底后的6张
        self.stir_events = []                   # 炒底事件序列
        self.tricks = []
        self.attacker_score = 0
        self.bottom_score = 0
        self.koudi = False
        self.koudi_multiplier = 0
        self.dealer_up = 0
        self.attacker_up = 0
        self.result = ''
        self.side_switch = False                # 是否上台
        self.winner_team = None                 # 过A获胜队伍
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)


# ==================== 游戏引擎 ====================

def settle_round(rec, state):
    """结算一局。就地更新 rec 的档位/扣底/结果, 并推进 state 的队伍等级/庄权。
    state 需提供: team_a_level / team_b_level / dealer_pid / game_over / winner。
    """
    sc = rec.attacker_score
    # 抠底
    if rec.tricks:
        lt = rec.tricks[-1]
        if lt['winner_side'] == 'attacker':
            rec.koudi = True
            rec.koudi_multiplier = koudi_multiplier(lt['winner_cards'], rec.level, rec.trump_suit)
            rec.bottom_score = sum(SCORE_VALUES.get(c.rank, 0) for c in rec.bottom)
            bonus = rec.bottom_score * rec.koudi_multiplier
            rec.attacker_score += bonus
            rec.log(f"【抠底】闲家赢最后一圈({PATTERN_CN.get(lt['pattern'], '?')}"
                    f"×{rec.koudi_multiplier}) 底分{rec.bottom_score} → 加{bonus}分")
    sc = rec.attacker_score

    # 升级档位
    if sc == 0:
        rec.dealer_up, rec.attacker_up, rec.result = 3, 0, '庄家升3级'
    elif sc <= 55:
        rec.dealer_up, rec.attacker_up, rec.result = 2, 0, '庄家升2级'
    elif sc <= 119:
        rec.dealer_up, rec.attacker_up, rec.result = 1, 0, '庄家升1级'
    elif sc <= 124:
        rec.dealer_up, rec.attacker_up, rec.result = 0, 0, '闲家上台'
    elif sc <= 175:
        rec.dealer_up, rec.attacker_up, rec.result = 0, 1, '闲家上台升1级'
    elif sc <= 235:
        rec.dealer_up, rec.attacker_up, rec.result = 0, 2, '闲家上台升2级'
    elif sc <= 295:
        rec.dealer_up, rec.attacker_up, rec.result = 0, 3, '闲家上台升3级'
    else:
        rec.dealer_up, rec.attacker_up, rec.result = 0, 3, '闲家上台升3级(封顶)'

    # 队伍等级
    dealer_is_a = rec.dealer_pid in (0, 2)
    if dealer_is_a:
        state.team_a_level, win_a = level_up(state.team_a_level, rec.dealer_up)
        state.team_b_level, win_b = level_up(state.team_b_level, rec.attacker_up)
    else:
        state.team_b_level, win_b = level_up(state.team_b_level, rec.dealer_up)
        state.team_a_level, win_a = level_up(state.team_a_level, rec.attacker_up)
    rec.team_a_level_after = state.team_a_level
    rec.team_b_level_after = state.team_b_level

    if win_a:
        state.winner, state.game_over = '队伍A', True
        rec.result = '队伍A过A获胜🏆'
    if win_b:
        state.winner, state.game_over = '队伍B', True
        rec.result = '队伍B过A获胜🏆'

    # 上台
    if sc >= 120 and not state.game_over:
        rec.side_switch = True
        state.dealer_pid = rec.at[0]


class Game:
    def __init__(self, max_rounds=None):
        self.team_a_level = '2'                 # 队伍A(玩家0,2)等级
        self.team_b_level = '2'                 # 队伍B(玩家1,3)等级
        self.dealer_pid = random.randint(0, 3)
        self.rnd = 0
        self.records = []
        self.winner = None
        self.game_over = False
        self.max_rounds = max_rounds

    def _deal(self, rec):
        deck = create_deck()
        random.shuffle(deck)
        hands = [[] for _ in range(4)]
        for i, card in enumerate(deck):
            (hands[i % 4] if i < 156 else rec.initial_bottom).append(card)
        rec.initial_bottom = list(rec.initial_bottom)
        rec.log(f"发牌完成 | 底牌: {cards_str(rec.initial_bottom)}")
        return hands

    def _play_round(self):
        self.rnd += 1
        dealer = self.dealer_pid
        dt = [dealer, (dealer + 2) % 4]
        at = [(dealer + 1) % 4, (dealer + 3) % 4]
        level = self.team_a_level if dealer in (0, 2) else self.team_b_level

        rec = RoundRecord(self.rnd, dealer, level, dt, at,
                          self.team_a_level, self.team_b_level)
        rec.log(f"=== 第{self.rnd}局 | 庄家=玩家{dealer+1} 打{level} ===")
        rec.log(f"庄家方: 玩{dt[0]+1}、玩{dt[1]+1} | 闲家方: 玩{at[0]+1}、玩{at[1]+1}")

        hands = self._deal(rec)

        # 叫主
        ts, calls = determine_trump(hands, level, dealer)
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

        # 庄家埋底: 收6张底牌, 再从手牌埋6张
        dealer_hand = hands[dealer] + rec.initial_bottom
        bot = Bot(dealer, dealer_hand, 'dealer', level, ts)
        buried = bot.select_for_bottom(6)
        hands[dealer] = list(bot.hand)
        rec.bury_pid = dealer
        rec.buried_cards = list(buried)
        rec.bottom = list(buried)
        bs = sum(SCORE_VALUES.get(c.rank, 0) for c in rec.bottom)
        rec.log(f"【埋底】庄家埋: {cards_str(buried)} (底分={bs})")

        # 闲家炒底
        stir_events, final_bottom = determine_stir(hands, level, dealer, list(rec.bottom), ts)
        rec.stir_events = stir_events
        rec.bottom = final_bottom
        rec.bottom_score = sum(SCORE_VALUES.get(c.rank, 0) for c in final_bottom)
        for e in stir_events:
            rec.log(f"【炒底】玩{e['pid']+1} 以{e['label']}炒底: "
                    f"换入 {cards_str(e['taken'])} | 弃出 {cards_str(e['discarded'])}")
        if stir_events:
            rec.log(f"炒底后底牌: {cards_str(rec.bottom)} (底分={rec.bottom_score})")
        else:
            rec.log("【炒底】无人炒底")

        for pid in range(4):
            assert len(hands[pid]) == 39, f"玩家{pid+1}手牌{len(hands[pid])}!=39"
        assert len(rec.bottom) == 6, f"底牌{len(rec.bottom)}!=6"

        # 出牌
        self._play_tricks(rec, hands)

        # 结算
        self._settle(rec)
        rec.log(f"第{self.rnd}局结束 | 闲家得分={rec.attacker_score} | {rec.result}")
        return rec

    def _play_tricks(self, rec, hands):
        bots = {}
        for pid in range(4):
            side = 'dealer' if pid in rec.dt else 'attacker'
            bots[pid] = Bot(pid, hands[pid], side, rec.level, rec.trump_suit)

        leader = rec.dealer_pid
        t = 0
        while any(bots[p].hand for p in range(4)) and t < 100:
            t += 1
            lead_cards = bots[leader].lead()
            if not lead_cards:
                break
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
            score = sum(SCORE_VALUES.get(c.rank, 0)
                        for _, cl in played for c in cl)
            trick = {'num': t, 'leader': leader, 'played': played,
                     'played_actions': actions, 'winner': best_pid, 'winner_side': winner_side,
                     'winner_cards': best_cards, 'pattern': pattern, 'score': score}
            rec.tricks.append(trick)

            parts = []
            for (pid, cl), a in zip(played, actions):
                tag = f"·{ACTION_CN[a]}" if a else ''
                parts.append(f"玩{pid+1}{tag}:{cards_str(cl)}")
            rec.log(f"第{t}圈[{PATTERN_CN.get(pattern, '?')}]: "
                    f"{' | '.join(parts)} → 赢:玩{best_pid+1}({cards_str(best_cards)}) +{score}分")
            leader = best_pid

        rec.attacker_score = sum(tr['score'] for tr in rec.tricks
                                 if tr['winner_side'] == 'attacker')

    def _settle(self, rec):
        settle_round(rec, self)
        rec.log(f"结算: 闲家得分={rec.attacker_score} "
                f"庄方+{rec.dealer_up} 闲方+{rec.attacker_up} | {rec.result}")

    def run(self):
        while not self.game_over:
            if self.max_rounds and self.rnd >= self.max_rounds:
                break
            self.records.append(self._play_round())
        return self.records


# ==================== CLI 测试 ====================

def main():
    import argparse
    p = argparse.ArgumentParser(description='三副牌升级模拟(CLI测试)')
    p.add_argument('--seed', type=int, default=None)
    p.add_argument('--rounds', type=int, default=200, help='模拟局数上限')
    args = p.parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    game = Game(max_rounds=args.rounds)
    game.run()
    print(f"共{len(game.records)}局 | 胜方: {game.winner or '未分胜负'}")
    print(f"最终等级: 队伍A={game.team_a_level} 队伍B={game.team_b_level}")
    # 统计
    from collections import Counter
    c = Counter(r.result for r in game.records)
    for k, v in c.most_common():
        print(f"  {k}: {v}局")
    # 打印最后一局日志
    print("\n--- 最后一局日志 ---")
    for line in game.records[-1].logs:
        print(line)


if __name__ == '__main__':
    main()
