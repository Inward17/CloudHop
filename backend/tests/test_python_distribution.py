from cloudhop.analyze import analyze_files


def test_regression():
    result = analyze_files([{'path': "requirements.txt", 'text': "fastapi\npsycopg2_binary==2.9.9\n"}])
    assert [store['kind'] for store in result['datastores']] == ['postgres']
