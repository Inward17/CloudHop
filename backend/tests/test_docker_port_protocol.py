from cloudhop.analyze import analyze_files


def test_regression():
    result = analyze_files([{'path': "Dockerfile", 'text': "FROM app\nEXPOSE 8125/udp 8080/tcp\n"}])
    assert result['services'][0]['port'] == 8080
