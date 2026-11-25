"""Try the assistant's model on a sentence, or measure it on its own dataset.

    python manage.py chatbot_check "add a gust named ravi 0501234567"
    python manage.py chatbot_check --eval

``--eval`` splits the sentences into ten parts and, for each part, trains on
the other nine and checks the answers — every sentence is tested once, on a
model that never saw it. "Wrong" is a sentence read as a different intent, which is
what to fix. "Not sure" is a sentence the model refused to guess; the
assistant then asks again, so that is safe. A one-word sentence ("yup") is
often "not sure" when held out, because no other sentence teaches its word.
"""
from collections import Counter

from django.core.management.base import BaseCommand

from assistant.brain import classifier, entities


class Command(BaseCommand):
    help = 'Show what the assistant understands from a sentence, or test it on its dataset.'

    def add_arguments(self, parser):
        parser.add_argument('text', nargs='*', help='A sentence to try.')
        parser.add_argument('--eval', action='store_true',
                            help='Test the model on dataset.json sentences it was not trained on.')

    def handle(self, *args, **options):
        if options['eval']:
            return self.evaluate()
        text = ' '.join(options['text'])
        if not text:
            self.stderr.write('Give a sentence, or --eval.')
            return
        intent, confidence = classifier.predict(text)
        self.stdout.write(f'intent      {intent}  ({confidence:.0%} sure)')
        for field, value in entities.extract(text).items():
            self.stdout.write(f'{field:<12}{value}')

    def evaluate(self):
        rows = classifier.load_dataset()
        total, right = Counter(), Counter()
        wrong, unsure = [], []
        folds = 10
        models = [classifier.train([row for i, row in enumerate(rows) if i % folds != fold])
                  for fold in range(folds)]
        for index, (intent, text) in enumerate(rows):
            got, confidence = classifier.predict(text, model=models[index % folds])
            total[intent] += 1
            if got == intent:
                right[intent] += 1
            elif got == classifier.UNKNOWN:
                unsure.append((intent, text))
            else:
                wrong.append((intent, text, got, confidence))

        self.stdout.write(f'{len(rows)} sentences, each tested on a model that never saw it\n')
        self.stdout.write(f'{"intent":<12}{"right":>7}{"of":>5}')
        for intent in sorted(total):
            self.stdout.write(f'{intent:<12}{right[intent]:>7}{total[intent]:>5}')
        self.stdout.write(
            f'\nright {sum(right.values())}, not sure {len(unsure)}, wrong {len(wrong)}')
        if wrong:
            self.stdout.write('\nWrong (add more sentences like these to the right list):')
            for intent, text, got, confidence in wrong:
                self.stdout.write(f'  "{text}"  is {intent}, read as {got} ({confidence:.0%})')
