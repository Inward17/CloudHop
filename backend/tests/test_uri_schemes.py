from cloudhop.analyze import analyze_files


def test_regression():
    result = analyze_files([{'path': ".env.example", 'text': "CACHE=rediss://example.test\nDB=mongodb+srv://example.test\n"}])
    assert [store['kind'] for store in result['datastores']] == ['redis', 'mongodb']
