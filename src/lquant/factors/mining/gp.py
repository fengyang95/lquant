"""GP 生成器：AST 交叉 + 变异，产 DSL 字符串（方案 M3d）。

验收纪律：同预算下跑赢 random 基线，否则不合并。
"""
from __future__ import annotations

import random

from lquant.factors.dsl.ast_nodes import BinaryOp, Call, Field, Num, UnaryOp
from lquant.factors.dsl.parser import parse
from lquant.factors.dsl.printer import unparse
from lquant.factors.mining.random_gen import _expr as _rand_expr


class GPGenerator:
    """种群驱动的 GP。generator 闭包形态与 random 相同（能吐字符串就能接）。"""

    def __init__(self, seed=None, pop_size=40, elite=6, p_mutation=0.35):
        self.rng = random.Random(seed)
        self.pop = []          # [(expr_str, fitness)]
        self.pop_size = pop_size
        self.elite = elite
        self.p_mutation = p_mutation
        self._seen = set()

    def _new_expr(self):
        from lquant.factors.mining.random_gen import make_generator

        return make_generator(self.rng.randint(0, 2**30))()

    def _mutate(self, node, force=False):
        if isinstance(node, Num):
            if force or self.rng.random() < 0.5:
                return Num(self.rng.choice([3.0, 5.0, 10.0, 20.0, 60.0]))
            return node
        if isinstance(node, Field):
            if force or self.rng.random() < 0.5:
                return Field(self.rng.choice(['close', 'open', 'volume', 'amount', 'vwap_x', 'high', 'low']))
            return node

        if isinstance(node, Call):
            kids = [self._mutate(a, force) for a in node.args]
            if force or self.rng.random() < self.p_mutation:
                kids[-1] = Num(self.rng.choice([3.0, 5.0, 10.0, 20.0, 60.0]))
            return Call(node.name, kids)
        if isinstance(node, BinaryOp):
            return BinaryOp(node.op, self._mutate(node.left, force), self._mutate(node.right, force))
        if isinstance(node, UnaryOp):
            return UnaryOp(node.op, self._mutate(node.arg, force))
        return node

    def _crossover(self, a, b):
        """单点交叉：把 b 的随机子树换进 a。"""
        import copy

        def collect(n, acc):
            acc.append(n)
            for c in getattr(n, "args", []) or []:
                collect(c, acc)
            for c in (getattr(n, "left", None), getattr(n, "right", None), getattr(n, "arg", None)):
                if c is not None:
                    collect(c, acc)
        acc = []
        collect(b, acc)
        donor = self.rng.choice(acc)
        # 替换 a 的随机位置
        def replace(n, depth):
            if depth == 0:
                return copy.deepcopy(donor)
            if isinstance(n, Call):
                return Call(n.name, [replace(x, depth - 1) if i == 0 else x for i, x in enumerate(n.args)])
            if isinstance(n, BinaryOp):
                return BinaryOp(n.op, replace(n.left, depth - 1), n.right)
            if isinstance(n, UnaryOp):
                return UnaryOp(n.op, replace(n.arg, depth - 1))
            return n
        return replace(a, self.rng.randint(0, 2))


    def __call__(self) -> str:
        """generator 闭包形态：返回候选表达式字符串。"""
        if len(self.pop) < 2:
            e = self._new_expr()
        else:
            self.pop.sort(key=lambda x: -x[1])
            pa = self.rng.choice(self.pop[: self.elite])
            pb = self.rng.choice(self.pop[: self.elite])
            ast = parse(pa[0], "gp")
            donor = parse(pb[0], "gp")
            child = self._crossover(ast.root, donor.root)
            child = self._mutate(child, force=self.rng.random() < 0.3)
            e = unparse(child)
        if e in self._seen:
            e = self._new_expr()
        self._seen.add(e)
        return e

    def feedback(self, expr: str, fitness: float) -> None:
        """runner 每评一个候选回传一次适应度，驱动进化。"""
        self.pop.append((expr, fitness))
        if len(self.pop) > self.pop_size * 2:
            self.pop.sort(key=lambda x: -x[1])
            self.pop = self.pop[: self.pop_size]
