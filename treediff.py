import argparse
from collections import defaultdict
import ctypes
from difflib import Differ
from glob import glob
import importlib
import json
import logging
from pprint import pformat
import sys

import farmhash
Node = importlib.import_module(
    "bblfsh.gopkg.in.bblfsh.sdk.v1.uast.generated_pb2").Node
from modelforge.logs import setup_logging
import numpy
import lapjv


def setup():
    parser = argparse.ArgumentParser()
    parser.add_argument("sequence")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--hash-rounds", type=int, default=10)
    args = parser.parse_args()
    setup_logging("INFO")
    return args


HASH_SIZE = 16


def hash_node(node, seed, mapping, white_list):
    def hash_self():
        if node.start_position.line == 0 or id(node) not in white_list:
            return 0, b""
        roles = list(node.roles)
        seed1 = 0
        for i in range(min(4, len(roles))):
            seed1 |= roles[i] << (i << 3)
        seed2 = 0
        if len(roles) > 4:
            for i in range(min(4, len(roles) - 4)):
                seed2 |= roles[i + 4] << (i << 3)
        h1, h2 = farmhash.hash128withseed(node.token, seed1, seed2)
        return 1, h1.to_bytes(8, "little") + h2.to_bytes(8, "little")

    children = list(node.children)
    if not children:
        h = hash_self()
        if h[0] > 0:
            mapping[id(node)] = h[1]
        return h
    inner_hashes = [hash_node(c, seed, mapping, white_list) for c in children] + [hash_self()]
    if id(node) not in white_list:
        return 0, b""
    weights = [h[0] for h in inner_hashes if h[0] > 0]
    total = sum(weights)
    if weights == 0:
        return 0, b""
    sample_sizes = []
    for w in weights:
        s = max(1, (w * HASH_SIZE) // total)
        sample_sizes.append(s)
    delta = HASH_SIZE - sum(sample_sizes)
    if delta != 0:
        ss2 = [(s, i) for i, s in enumerate(sample_sizes)]
        ss2.sort(reverse=True)
        fix = 1 if delta > 0 else -1
        possible = True
        while delta != 0 and possible:
            possible = False
            for s, i in ss2:
                if sample_sizes[i] < 2 and fix < 0:
                    continue
                possible = True
                sample_sizes[i] += fix
                delta -= fix
                if delta == 0:
                    break
        if possible:
            for s in sample_sizes:
                assert s > 0
        else:
            # random sample 15 nodes
            indices = list(range(len(sample_sizes) - 1))
            numpy.random.seed(seed)
            choice = numpy.random.choice(indices, 15, replace=False)
            for i in range(len(sample_sizes)):
                sample_sizes[i] = 0
            for i in choice:
                sample_sizes[i] = 1
            sample_sizes[-1] = 1  # self
    choices = []
    for h, s in zip(inner_hashes, sample_sizes):
        if h[0] == 0:
            continue
        numpy.random.seed(seed)
        choice = numpy.random.choice(
            numpy.array(sorted(h[1]), dtype=numpy.uint8), s, replace=False)
        choices.append(choice)
    choices = numpy.hstack(choices)
    choices = choices.tobytes()
    mapping[id(node)] = choices
    return total, choices


def map_parents(node, parents):
    for child in node.children:
        parents[id(child)] = id(node)
        map_parents(child, parents)


def dereference_idptr(value):
    return ctypes.cast(value, ctypes.py_object).value


class Line2Nodes:
    def __init__(self, nlines, root):
        self._lines = lines = [[] for _ in range(nlines)]
        queue = [root]
        while queue:
            node = queue.pop()
            if node.start_position.line > 0:
                for line in range(node.start_position.line, node.end_position.line + 1):
                    lines[line - 1].append(node)
            queue.extend(node.children)

    def __getitem__(self, line):
        if isinstance(line, tuple):
            start, end = line
            nodes = set(id(n) for n in self._lines[start])
            if start > 0:
                nodes -= set(id(n) for n in self._lines[start - 1])
            for l in range(start + 1, end):
                nodes = nodes.union(set(id(n) for n in self._lines[l]))
            if end < len(self._lines):
                nodes -= set(id(n) for n in self._lines[end])
            return nodes
        return self._lines[line]


def adjust_seqdiff(seqdiff, src_after, line2nodes):
    log = logging.getLogger("treediff")
    line_before = 0
    line_after = 0
    intervals = []
    interval_add = []
    interval_rm = []
    interval_start_before = -1
    interval_start_after = -1
    for i, line in enumerate(seqdiff):
        if line[0] == "+":
            if len(interval_add) == 0:
                interval_start_after = line_after
            line_after += 1
            interval_add.append(line)
        elif line[0] == "-":
            if len(interval_rm) == 0:
                interval_start_before = line_before
            line_before += 1
            interval_rm.append(line)
        else:
            if interval_add or interval_rm:
                intervals.append((
                    interval_add, interval_rm,
                    interval_start_before if interval_start_before > -1 else line_before,
                    line_before,
                    interval_start_after if interval_start_after > -1 else line_after,
                    line_after,
                ))
                interval_add = []
                interval_rm = []
            line_before += 1
            line_after += 1
    adjusted_diff = []
    for i in intervals:
        interval_add, interval_rm, start_before, end_before, start_after, end_after = i
        if len(interval_add) in (0, 1) and len(interval_rm) in (0, 1):
            adjusted_diff.append(i[2:])
            continue
        neighbors = 0
        while neighbors < len(interval_add) and \
                src_after[end_after + neighbors] == interval_add[neighbors][2:]:
            neighbors += 1
        if neighbors == 0:
            adjusted_diff.append(i[2:])
            continue
        ls1 = set()
        ls2 = set()
        for l in range(start_after, end_after):
            ls1.update(map(id, line2nodes[l]))
        for l in range(start_after + neighbors, end_after + neighbors):
            ls2.update(map(id, line2nodes[l]))
        if len(ls1) == len(ls2):
            adjusted_diff.append(i[2:])
            continue
        log.info("adjusted %s", i[2:])
        if end_before - start_before > 0:
            adjusted_diff.append((start_before, end_before, start_after, start_after))
        end_before += neighbors
        start_after += neighbors
        end_after += neighbors
        adjusted_diff.append((end_before, end_before, start_after, end_after))
    return adjusted_diff


def treediff(src1, uast1, src2, uast2, nseeds=10):
    log = logging.getLogger("treediff")

    log.info("line2nodes")
    src1 = src1.splitlines(True)
    src2 = src2.splitlines(True)
    line2nodes_before = Line2Nodes(len(src1), uast1)
    line2nodes_after = Line2Nodes(len(src2), uast2)

    log.info("regular diff")
    differ = Differ()
    seqdiff = list(differ.compare(src1, src2))
    adjusted_diff = adjust_seqdiff(seqdiff, src2, line2nodes_after)
    lines_before = [i[:2] for i in adjusted_diff]
    lines_after = [i[2:] for i in adjusted_diff]

    def filter_changed(lines, line2node):
        preserved = set()
        for lr in lines:
            preserved.update(line2node[lr])
        return preserved

    white_list1 = filter_changed(lines_before, line2nodes_before)
    white_list2 = filter_changed(lines_after, line2nodes_after)

    if len(white_list1) == 0:
        diff = []
        for k in white_list2:
            diff.append(("add", dereference_idptr(k)))
        return diff
    if len(white_list2) == 0:
        diff = []
        for k in white_list1:
            diff.append(("delete", dereference_idptr(k)))
        return diff

    dists = None
    supermap1 = defaultdict(bytes)
    supermap2 = defaultdict(bytes)
    for seed in range(nseeds):
        log.info("hash round %d/%d", seed + 1, nseeds)
        map1 = {}
        map2 = {}
        hash_node(uast1, seed, map1, white_list1)
        hash_node(uast2, seed, map2, white_list2)

        if dists is None:
            log.info("nodes before: %d", len(map1))
            log.info("nodes after: %d", len(map2))
            dists = numpy.ones((len(map1) + len(map2),) * 2, dtype=numpy.float32)
            dists *= 2 * HASH_SIZE * nseeds
            dists[:len(map1), len(map1):] = HASH_SIZE * nseeds
            dists[len(map1):, :len(map1)] = HASH_SIZE * nseeds
        byte_matches = [[] for _ in range(256)]
        for i, (k, h) in enumerate(map2.items()):
            supermap2[k] += h
            for b in set(h):
                byte_matches[b].append(i)
        for i, (k, h) in enumerate(map1.items()):
            supermap1[k] += h
            candidates = [0] * len(map2)
            for b in h:  # no set here!
                for j in byte_matches[b]:
                    candidates[j] -= 1
            dists[i, len(map1):] += candidates
            dists[len(map1):, i] += candidates

    seq1 = list(map1)
    seq2 = list(map2)

    log.info("lapjv")
    row_ind, _, _ = lapjv.lapjv(dists)
    log.info("compiling edit script")
    threshold = HASH_SIZE / 2
    deleted = {(i, j) for (i, j) in enumerate(row_ind[:len(map1)])
               if j < len(map1) or dists[i, j] > threshold * nseeds}
    exact = {(i, j) for (i, j) in enumerate(row_ind[:len(map1)])
             if j >= len(map1) and supermap1[seq1[i]] == supermap2[seq2[j - len(map1)]]}
    mapped2 = {p[1] for p in (set(enumerate(row_ind[:len(map1)])) - deleted)}
    added = set(range(len(map1), len(row_ind))) - mapped2
    log.info("deleted: %d", len(deleted))
    log.info("added: %d", len(added))
    log.info("match: %d (exact %d, fuzzy %d)",
             len(map1) - len(deleted),
             len(exact),
             len(map1) - len(deleted) - len(exact))
    diff = []
    for i, _ in deleted:
        node = dereference_idptr(seq1[i])
        diff.append(("delete", node))
    for j in added:
        node = dereference_idptr(seq2[j - len(map1)])
        diff.append(("add", node))
    for i, j in set(enumerate(row_ind[:len(map1)])) - deleted - exact:
        diff.append(("modify", dereference_idptr(seq1[i]), dereference_idptr(seq2[j - len(map1)])))
    return diff


def write_diff(src_before, src_after, diff, output):
    log = logging.getLogger("treediff")
    DESCRIPTOR = importlib.import_module(
        "bblfsh.gopkg.in.bblfsh.sdk.v1.uast.generated_pb2").DESCRIPTOR
    Role = DESCRIPTOR.enum_types_by_name["Role"]
    file_role = Role.values_by_name["FILE"].number
    script = []
    for change in diff:
        action, node = change[:2]
        if file_role in node.roles:
            continue
        if not node.start_position.line:
            log.warning("node \"%s\" [%s] with %d children has no position, skipped",
                        node.token, ", ".join(Role.values_by_number[n].name for n in node.roles),
                        len(node.children))
            continue

        def format_position(pos):
            return {"line": pos.line, "col": pos.col, "offset": pos.offset}

        if action in ("add", "delete"):
            script.append((action, format_position(node.start_position),
                           format_position(node.end_position)))
        else:
            script.append((action, {
                "before": [format_position(change[1].start_position),
                           format_position(change[1].end_position)],
                "after": [format_position(change[2].start_position),
                          format_position(change[2].end_position)]}))
    log.info("SCRIPT:\n%s", pformat(script))
    with open(output, "w") as fout:
        json.dump({"src_before": src_before,
                   "src_after": src_after,
                   "script": script},
                  fout, sort_keys=True, indent=2)


def main():
    args = setup()
    base = args.sequence
    uast_before = glob("%s_before_*.pb" % base)[0]
    uast_after = glob("%s_after_*.pb" % base)[0]
    src_before = glob("%s_before_*.src" % base)[0]
    src_after = glob("%s_after_*.src" % base)[0]
    with open(src_before) as fin:
        src_before = fin.read()
    with open(src_after) as fin:
        src_after = fin.read()
    with open(uast_before, "rb") as fin:
        uast1 = Node.FromString(fin.read())
    with open(uast_after, "rb") as fin:
        uast2 = Node.FromString(fin.read())
    diff = treediff(src_before, uast1, src_after, uast2, nseeds=args.hash_rounds)
    write_diff(src_before, src_after, diff, args.output)


if __name__ == "__main__":
    sys.exit(main())
