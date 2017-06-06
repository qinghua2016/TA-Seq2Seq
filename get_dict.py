import cPickle
import codecs
def load_file(input_file):
    input_data = codecs.open(input_file, 'r', 'utf-8')
    input_text = input_data.read()
    return input_text
text=load_file('dict.txt').split()
vocab=dict()
for i in xrange(len(text)):
    vocab[text[i]]=i
with open('src_vocab.pkl', 'wb') as f:
    cPickle.dump(vocab, f)
