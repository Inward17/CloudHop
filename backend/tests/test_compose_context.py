from cloudhop.analyze import analyze_files
from cloudhop.generate import compose_for


def test_build_context_is_a_quoted_yaml_scalar():
    services = analyze_files([{'path': 'api #2/Dockerfile', 'text': 'FROM sample'}])
    assert 'build: "./src/api #2"' in compose_for(services)
