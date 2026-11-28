from cloudhop.analyze import analyze_files
from cloudhop.generate import deploy_issues


def test_colliding_paths():
    services = analyze_files([{'path': path + '/Dockerfile', 'text': 'FROM sample'} for path in ('foo/bar', 'foo-bar')])
    assert any('app-foo-bar' in issue for issue in deploy_issues(services))
