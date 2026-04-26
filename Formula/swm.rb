# Homebrew formula for swm — Cloud GPU workflow manager
# To use: brew tap <your-org>/swm && brew install swm
#
# After a release, update the url, version, and sha256 values below.
# The sha256 files are published alongside each binary in GitHub Releases.

class Swm < Formula
  desc "One CLI to search, provision, and manage cloud GPUs across 10 providers"
  homepage "https://github.com/swm-gpu/swm"
  version "0.1.0"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/swm-gpu/swm/releases/download/v#{version}/swm-#{version}-darwin-arm64"
      sha256 "e05b409e40b626976340579f86c1029c5355a7a06b2d761c007bd238de40ae58"
    end
    on_intel do
      url "https://github.com/swm-gpu/swm/releases/download/v#{version}/swm-#{version}-darwin-amd64"
      sha256 "PLACEHOLDER_SHA256_AMD64"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/swm-gpu/swm/releases/download/v#{version}/swm-#{version}-linux-amd64"
      sha256 "PLACEHOLDER_SHA256_LINUX"
    end
  end

  def install
    binary = Dir["swm-*"].first || "swm"
    bin.install binary => "swm"
  end

  test do
    assert_match "swm, version", shell_output("#{bin}/swm --version")
  end
end
