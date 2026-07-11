def check_scores(scores):
    passed = []
    for name, score in scores.items():
        if score >= 60:
            passed.append(name)
        else:
            print(name + " 没及格，考了 " + str(score))
    return passed


results = {"小明": 85, "小红": 52, "小刚": 60}
winners = check_scores(results)
print("及格的有：", winners)
