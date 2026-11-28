from cloudhop.analyze import analyze_files


def test_regression():
    result = analyze_files([{'path': "compose.yaml", 'text': "services:\n  db:\n    image: registry.example:5000/team/postgres:16\n"}])
    assert [store['kind'] for store in result['datastores']] == ['postgres']
