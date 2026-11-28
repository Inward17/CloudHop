from cloudhop.analyze import analyze_files


def test_regression():
    result = analyze_files([{'path': "requirements.txt", 'text': "# dependency list\nFastAPI[standard]\n"}])
    assert result['services'][0]['evidence'][0]['line'] == 2
