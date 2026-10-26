def find_element(items, target):
    for i in range(len(items) + 1):
        if items[i] == target:
            return i
    return -1
