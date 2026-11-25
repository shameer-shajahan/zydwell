"""The assistant's intent model: multinomial Naive Bayes, in plain Python.

It learns from ``dataset.json`` (sentences labelled with what they mean) the
first time it is used, which takes a few milliseconds, and then lives in memory
for the life of the process. No API, no download, no extra library.

Each sentence becomes a bag of features:

* its words (``w:guest``),
* pairs of neighbouring words (``b:add_guest``), so "add guest" and
  "search guest" are told apart,
* three-letter pieces of every word (``c:#gu``, ``c:gue`` ...), so a typo such
  as "gust" or "guset" still shares most of its pieces with "guest".

Naive Bayes then asks, for every intent: how likely are these features if the
sentence meant that? The most likely intent wins. A sentence whose words the
model has never seen is reported as ``unknown`` rather than guessed.
"""
import json
import math
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

DATASET = Path(__file__).with_name('dataset.json')
UNKNOWN = 'unknown'
#: Below this the answer is too close to call and is reported as ``unknown``.
THRESHOLD = 0.55
#: The three kinds of feature do not count the same: there are many more
#: letter pieces than words, and a bare piece is weaker evidence.
WEIGHTS = {'w': 1.0, 'b': 1.5, 'c': 0.35}
#: Naive Bayes grows over-sure as a sentence gets longer, because every feature
#: multiplies in. Scores are softened by one step per this many features.
FEATURES_PER_STEP = 8

_WORD = re.compile(r"[a-z]+")


def words(text):
    return _WORD.findall((text or '').lower())


def features(text):
    tokens = words(text)
    out = []
    for word in tokens:
        out.append('w:' + word)
        padded = f'#{word}#'
        out.extend('c:' + padded[i:i + 3] for i in range(len(padded) - 2))
    out.extend(f'b:{a}_{b}' for a, b in zip(tokens, tokens[1:]))
    return out


def load_dataset(path=DATASET):
    with open(path, encoding='utf-8') as handle:
        data = json.load(handle)
    return [(intent, text) for intent, texts in data['examples'].items() for text in texts]


def train(rows):
    """``rows`` is ``[(intent, text), ...]``; returns the model as a dict."""
    counts = defaultdict(Counter)
    docs = Counter()
    for intent, text in rows:
        docs[intent] += 1
        counts[intent].update(features(text))
    vocab = set()
    for bag in counts.values():
        vocab.update(bag)
    total_docs = sum(docs.values())
    exact = defaultdict(set)
    for intent, text in rows:
        exact[' '.join(words(text))].add(intent)
    model = {'priors': {}, 'log_probs': {}, 'unseen': {}, 'vocab': vocab,
             'words': {f[2:] for f in vocab if f.startswith('w:')},
             # A sentence taught word for word needs no guessing.
             'exact': {key: found.pop() for key, found in exact.items() if len(found) == 1}}
    for intent, bag in counts.items():
        size = sum(bag.values())
        denominator = size + len(vocab)           # Laplace smoothing: +1 for every feature
        model['priors'][intent] = math.log(docs[intent] / total_docs)
        model['log_probs'][intent] = {
            feature: math.log((n + 1) / denominator) for feature, n in bag.items()}
        model['unseen'][intent] = math.log(1 / denominator)
    return model


def scores(model, text, among=None):
    """Confidence for each intent (they add up to 1)."""
    feats = [f for f in features(text) if f in model['vocab']]
    intents = [i for i in model['priors'] if among is None or i in among]
    if not feats or not intents:
        return {}
    raw = {}
    for intent in intents:
        table, unseen = model['log_probs'][intent], model['unseen'][intent]
        raw[intent] = model['priors'][intent] + sum(
            WEIGHTS[f[0]] * table.get(f, unseen) for f in feats)
    top = max(raw.values())
    temperature = max(1.0, len(feats) / FEATURES_PER_STEP)
    exp = {i: math.exp((s - top) / temperature) for i, s in raw.items()}
    total = sum(exp.values())
    return {i: v / total for i, v in exp.items()}


def predict(text, among=None, model=None):
    """``(intent, confidence)``; ``among`` limits the answer to some intents.

    A sentence with no word the model knows is ``unknown`` however its letter
    pieces happen to score; a name such as "Ravi" must not read as a command.
    """
    model = model or get_model()
    taught = model['exact'].get(' '.join(words(text)))
    if taught and (among is None or taught in among):
        return taught, 1.0
    result = scores(model, text, among)
    if not result:
        return UNKNOWN, 0.0
    intent = max(result, key=result.get)
    confidence = result[intent]
    known = any(w in model['words'] for w in words(text))
    if confidence < THRESHOLD or (not known and confidence < 0.9):
        return UNKNOWN, confidence
    return intent, confidence


@lru_cache(maxsize=1)
def get_model():
    return train(load_dataset())


@lru_cache(maxsize=1)
def vocabulary():
    """Every word in the training sentences — none of them is part of a name."""
    return frozenset(get_model()['words'])
