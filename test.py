import os

for x in range(86858383, 7378395394):
    print(str(x))
    os.system(r'python generators.py '+str(x))
    x = x + 1
    