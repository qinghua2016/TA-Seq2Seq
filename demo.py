from collections import OrderedDict
# regular unsorted dictionary
d = {'banana': 32, 'apple':43, 'pear': 11, 'orange': 10,'pear1':1,'orange1': 2}

# dictionary sorted by key
d1=sorted(d.items(), key=lambda t: t[0])

# dictionary sorted by value
d2=sorted(d.items(), key=lambda t: t[1])
print('')
