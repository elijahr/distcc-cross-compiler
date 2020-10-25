#!/bin/bash

set -uxeo pipefail

expected_arch=$1

main () {
  arch=$(uname -m)

  echo ">>> uname -m >>>"
  uname -m

  case $expected_arch in
    amd64) test $arch == x86_64 ;;
    i386) test $arch == i686 ;;
    arm32v6|arm32v7) test $arch == arm ;;
    arm64v8) test $arch == aarch64 ;;
    ppc64le) test $arch == ppc64el ;;
    s390x) test $arch == s390x ;;
  esac

  cd $(dirname $0)

  # gcc/etc should use ccache first
  test "$(which gcc)" == "/usr/lib/ccache/gcc" || test "$(which gcc)" == "/usr/lib/ccache/bin/gcc"
  test "$(which g++)" == "/usr/lib/ccache/g++" || test "$(which g++)" == "/usr/lib/ccache/bin/g++"
  test "$(which cc)" == "/usr/lib/ccache/cc" || test "$(which cc)" == "/usr/lib/ccache/bin/cc"

  # Assert that ccache wrappers wrap distcc wrappers
  [[ "$((gcc 2>&1 || true) | tail -n 1)" =~ ^distcc[\[0-9\]+] ]]

  # Print ccache config
  ccache -p

  rm -Rf /tmp/cJSON
  mkdir /tmp/cJSON
  tar xzf cJSON-master.tar.gz -C /tmp/cJSON
  cd /tmp/cJSON/cJSON-master

  # Compile cJSON twice
  make clean
  make test
  make clean
  make test
}

main
