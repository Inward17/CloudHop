from cloudhop.analyze import analyze_files


def test_equivalent_distribution_spellings_match_and_point_at_their_line():
    result = analyze_files([{'path': 'requirements.txt', 'text': 'fastapi\npsycopg2_binary==2.9.9\nRedis.Py-Cluster\nPyMongo>=4\n'}])
    assert [store['kind'] for store in result['datastores']] == ['postgres', 'mongodb']
    evidence = {store['kind']: store['evidence'][0] for store in result['datastores']}
    # The line is where the file spells it, not where the normalized name would be found.
    assert (evidence['postgres']['line'], evidence['postgres']['detail']) == (2, 'psycopg2-binary')
    assert evidence['mongodb']['line'] == 4
    assert result['services'][0]['evidence'][0]['line'] == 1
