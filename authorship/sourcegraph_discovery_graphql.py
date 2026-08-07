"""GraphQL document used by the frozen Sourcegraph discovery executor."""

SEARCH_RESULT_FRAGMENTS = """
fragment FileMatchFields on FileMatch {
  repository { name url }
  file { name path url commit { oid } }
  lineMatches { preview lineNumber offsetAndLengths limitHit }
}
fragment CommitSearchResultFields on CommitSearchResult {
  messagePreview { value highlights { line character length } }
  diffPreview { value highlights { line character length } }
  commit {
    repository { name }
    oid
    url
    subject
    author { date person { name email displayName } }
    committer { date person { name email displayName } }
  }
}
""".strip()

SEARCH_RESULTS_SELECTION = """
results {
  results {
    __typename
    ... on FileMatch { ...FileMatchFields }
    ... on CommitSearchResult { ...CommitSearchResultFields }
  }
  limitHit
  cloning { name }
  missing { name }
  timedout { name }
  resultCount
  elapsedMilliseconds
}
""".strip()

SEARCH_GRAPHQL = (
    SEARCH_RESULT_FRAGMENTS
    + """
query FrozenDiscoverySearch($query: String!) {
  search(query: $query) {
"""
    + SEARCH_RESULTS_SELECTION
    + """
  }
}
"""
)
