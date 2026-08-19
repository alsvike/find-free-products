// These public values point the dashboard at its GitHub data source.
// The dashboard then reads nightly CSV updates directly from GitHub, avoiding
// a new Netlify production deploy (and its credit cost) for every data refresh.
window.GRATISJAGTEN_CONFIG = {
  githubOwner: "alsvike",
  githubRepo: "find-free-products",
  githubBranch: "main",
};
