# The Homebrew formula, at the path that makes this repository a tap:
#   brew tap BrunoCremaFerreira/rhizome-graph https://github.com/BrunoCremaFerreira/rhizome-graph
#   brew install BrunoCremaFerreira/rhizome-graph/rhizome-graph
#
# Two decisions here are not style, and both come from the same fact: the built
# front end is gitignored.
#
#   1. `url` fetches a RELEASE ASSET, never a tag archive. The line every
#      formula starts with -- ".../archive/refs/tags/v1.0.tar.gz" -- is
#      generated from the git tree, so it carries no web/dist at all. It
#      downloads, builds and installs perfectly and serves a blank page,
#      because the daemon treats a missing front end as "serve the WebSocket
#      alone" rather than as an error. The asset named below is what
#      `python -m build` produces, and it carries the page inside the import
#      package at rhizome_graph/web/ -- the one place
#      rhizome_graph/assets.py looks on an installed system.
#   2. Nothing here builds the front end. No Node, no bundler, no registry: it
#      would put a JavaScript toolchain on every user's machine to reproduce an
#      artifact the release already contains, take minutes, fail on a network
#      that cannot reach the registry, and serve a bundle nobody tested.
#
# Unverified, and it has to be said rather than implied: no `brew` and no ruby
# exist on the machine this was written on, so `brew audit --strict --online`,
# `brew install --build-from-source` and `brew test` have never been run against
# it. tests/test_homebrew_formula.py reads it as text and says so at length.
class RhizomeGraph < Formula
  include Language::Python::Virtualenv

  desc "Real-time visualizer of what each Claude Code agent is doing"
  homepage "https://github.com/BrunoCremaFerreira/rhizome-graph"
  url "https://github.com/BrunoCremaFerreira/rhizome-graph/releases/download/v0.0.0/rhizome_graph-0.0.0.tar.gz"
  version "0.0.0"
  # A placeholder, deliberately obvious. There is no release yet, so no real
  # digest exists; zeros make `brew install` fail loudly on the mismatch, where
  # a plausible-looking wrong digest would read as done and never be revisited.
  # Replace it at release time with what `brew fetch` prints.
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  # The virtualenv is built against this interpreter, and the bound matters for
  # the same reason it does in debian/control: a virtualenv runs under the minor
  # version it was built for and under no other.
  depends_on "python@3.12"

  # On macOS nothing installs these for us -- there is no distribution package
  # to lean on, the way the Debian package leans on python3-watchdog -- so both
  # arrive as resources or the daemon dies on its first import.
  #
  # The websockets version is the one dependency floor that is not cosmetic:
  # daemon/server.py imports websockets.asyncio.server, which first ships in
  # the 13 series. tests/test_packaging.py measured it -- websockets/asyncio/
  # holds 0 files in 12.0 and 8 in 13.0.
  resource "websockets" do
    url "https://files.pythonhosted.org/packages/e2/73/9223dbc7be3dcaf2a7bbf756c351ec8da04b1fa573edaf545b95f6b0c7fd/websockets-13.1.tar.gz"
    sha256 "a3b3366087c1bc0a2795111edcadddb8b3b59509d5db5d7ea3fdd69f954a8878"
  end

  resource "watchdog" do
    url "https://files.pythonhosted.org/packages/db/7d/7f3d619e951c88ed75c6037b246ddcf2d322812ee8ea189be89511721d54/watchdog-6.0.0.tar.gz"
    sha256 "9ddf7c82fda3ae8e24decda1338ede66e1c99883db93711d8fb941eaa2d8c282"
  end

  def install
    # This installs the project's own distribution, which is what carries
    # rhizome_graph/web/. Nothing copies the page anywhere else on purpose:
    # installed into libexec/web, into pkgshare or into prefix/"web" it would be
    # bytes the daemon never opens, indistinguishable from not shipping it.
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      Attribution is opt-in. Copy the PostToolUse block into the .claude/settings.json
      of the project you want to watch, naming this command:

        #{opt_bin}/rhi-hook

      Without it every event arrives with no author, no figure appears, and the
      graph looks alive and unattended -- which is indistinguishable from nobody
      working right now.

      git is used for the diff view and the uncommitted-changes panel only; the
      graph does not need it.
    EOS
  end

  test do
    # Both commands, because rhi-hook is the one that gets forgotten and it is
    # as load-bearing as the launcher: it is what a settings file elsewhere
    # names, and it fires on every tool call.
    assert_match "rhi", shell_output("#{bin}/rhi --version")

    # The adapter's contract is silence and a zero exit whatever arrives, with
    # no daemon listening anywhere near this test.
    assert_equal "", pipe_output("#{bin}/rhi-hook", "{\"tool_name\":\"Read\"}", 0)
  end
end
