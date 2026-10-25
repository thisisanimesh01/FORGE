def binary_search(arr, target):
    """Search for target in sorted array arr."""
    left = 0
    # BUG: right bound set to len(arr) and condition <= allows arr[len(arr)] access
    right = len(arr)
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1
