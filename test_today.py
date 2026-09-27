"""Smoke tests for null GitHub repository edges. No ACCESS_TOKEN required."""
import hashlib
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault('ACCESS_TOKEN', 'test-token')
os.environ.setdefault('USER_NAME', 'test-user')

import today


def repo_edge(name, stars=None, commits=0):
    node = {
        'nameWithOwner': name,
        'defaultBranchRef': {'target': {'history': {'totalCount': commits}}},
    }
    if stars is not None:
        node['stargazers'] = {'totalCount': stars}
    return {'node': node}


def cache_line(name, commits, my_commits, added, deleted):
    repo_hash = hashlib.sha256(name.encode('utf-8')).hexdigest()
    return f'{repo_hash} {commits} {my_commits} {added} {deleted}\n'


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = ''

    def json(self):
        return self._payload


def repositories_payload(edges, total_count, has_next_page=False, end_cursor=None):
    return {
        'data': {
            'user': {
                'repositories': {
                    'totalCount': total_count,
                    'edges': edges,
                    'pageInfo': {
                        'hasNextPage': has_next_page,
                        'endCursor': end_cursor,
                    },
                }
            }
        }
    }


class StarsFromEdgesTests(unittest.TestCase):
    def test_skips_null_and_incomplete_edges(self):
        edges = [
            repo_edge('owner/alpha', stars=4),
            None,
            {'node': None},
            {'node': {}},
            {'node': {'nameWithOwner': 'owner/beta', 'stargazers': None}},
            {'node': {'stargazers': {'totalCount': 3}}},
            repo_edge('owner/gamma', stars=0),
            {'node': {'nameWithOwner': 'owner/delta', 'stargazers': {'totalCount': None}}},
        ]
        self.assertEqual(today.stars_from_edges(edges), 7)
        self.assertEqual(today.stars_from_edges(None), 0)

    def test_repository_node_requires_a_name(self):
        self.assertIsNone(today.repository_node(None))
        self.assertIsNone(today.repository_node({'node': None}))
        self.assertIsNone(today.repository_node({'node': {'stargazers': {'totalCount': 1}}}))
        node = today.repository_node(repo_edge('owner/alpha', stars=1))
        self.assertEqual(node['nameWithOwner'], 'owner/alpha')


class GraphReposStarsTests(unittest.TestCase):
    def test_star_count_paginates_and_skips_null_nodes(self):
        pages = [
            repositories_payload(
                [
                    repo_edge('owner/alpha', stars=2),
                    None,
                    {'node': None},
                    {'node': {'nameWithOwner': 'owner/beta', 'stargazers': None}},
                ],
                total_count=4,
                has_next_page=True,
                end_cursor='cursor-1',
            ),
            repositories_payload(
                [repo_edge('owner/gamma', stars=5)],
                total_count=4,
                has_next_page=False,
            ),
        ]
        responses = [FakeResponse(page) for page in pages]
        with mock.patch('today.requests.post', side_effect=responses) as post:
            stars = today.graph_repos_stars('stars', ['OWNER'])
        self.assertEqual(stars, 7)
        self.assertEqual(post.call_count, 2)
        second_cursor = post.call_args_list[1].kwargs['json']['variables']['cursor']
        self.assertEqual(second_cursor, 'cursor-1')

    def test_repo_count_still_uses_total_count(self):
        payload = repositories_payload([], total_count=12)
        with mock.patch('today.requests.post', return_value=FakeResponse(payload)):
            self.assertEqual(today.graph_repos_stars('repos', ['OWNER']), 12)


class LocCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def cache_path(self):
        name = hashlib.sha256(today.USER_NAME.encode('utf-8')).hexdigest()
        return os.path.join('cache', name + '.txt')

    def test_null_edges_do_not_drop_cached_loc_for_valid_repos(self):
        edges = [
            repo_edge('owner/alpha', commits=3),
            None,
            {'node': None},
            repo_edge('owner/beta', commits=4),
        ]
        os.makedirs('cache')
        with open(self.cache_path(), 'w') as handle:
            handle.write('Comment Block\n')
            handle.write(cache_line('owner/alpha', 3, 3, 100, 10))
            handle.write('0 0 0 0 0\n')
            handle.write('0 0 0 0 0\n')
            handle.write(cache_line('owner/beta', 4, 2, 50, 5))

        with mock.patch('today.recursive_loc', side_effect=AssertionError('network')):
            loc_add, loc_del, loc_net, cached = today.cache_builder(edges, 1, False)

        self.assertEqual([loc_add, loc_del, loc_net, cached], [150, 15, 135, True])

    def test_flush_keeps_a_slot_for_null_edges(self):
        edges = [
            repo_edge('owner/alpha', commits=0),
            {'node': None},
            repo_edge('owner/beta', commits=0),
        ]
        with mock.patch('today.recursive_loc', side_effect=AssertionError('network')):
            loc_add, loc_del, loc_net, cached = today.cache_builder(edges, 1, True)

        self.assertEqual([loc_add, loc_del, loc_net, cached], [0, 0, 0, False])
        with open(self.cache_path()) as handle:
            lines = handle.readlines()
        self.assertEqual(lines[0], 'Comment Block\n')
        self.assertEqual(lines[1], cache_line('owner/alpha', 0, 0, 0, 0))
        self.assertEqual(lines[2], '0 0 0 0 0\n')
        self.assertEqual(lines[3], cache_line('owner/beta', 0, 0, 0, 0))

    def test_loc_query_treats_null_edge_list_as_empty(self):
        payload = repositories_payload(None, total_count=0)
        with mock.patch('today.requests.post', return_value=FakeResponse(payload)):
            loc_add, loc_del, loc_net, cached = today.loc_query(['OWNER'], comment_size=1)
        self.assertEqual([loc_add, loc_del, loc_net, cached], [0, 0, 0, True])
        with open(self.cache_path()) as handle:
            self.assertEqual(handle.readlines(), ['Comment Block\n'])


class SvgOverwriteTests(unittest.TestCase):
    def test_valid_counts_still_update_svg(self):
        svg = '''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg">
  <text id="commit_data">0</text>
  <text id="star_data">0</text>
  <text id="repo_data">0</text>
  <text id="contributed_data">0</text>
  <text id="follower_data">0</text>
  <text id="sol_data">0</text>
  <text id="loc_data">0</text>
  <text id="loc_add">0++</text>
  <text id="loc_del">0--</text>
</svg>
'''
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'stats.svg')
            with open(path, 'w') as handle:
                handle.write(svg)
            today.svg_overwrite(path, 12, 7, 4, 2, 9, 100, [150, 15, 135, True])
            with open(path) as handle:
                text = handle.read()
        self.assertIn('>12<', text)
        self.assertIn('>7<', text)
        self.assertIn('>4<', text)
        self.assertIn('>135<', text)
        self.assertIn('>150++<', text)
        self.assertIn('>15--<', text)


if __name__ == '__main__':
    unittest.main()
