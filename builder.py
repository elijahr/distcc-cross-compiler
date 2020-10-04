#!/usr/bin/env python3

"""
Generate various docker and config files across a matrix of archs/distros.
"""

import abc
import argparse
import functools
import itertools
import os
import pathlib
import re
import shutil
import sys
import time

from path import Path
from sh import docker as _docker  # pylint: disable=no-name-in-module
from sh import docker_compose as _docker_compose  # pylint: disable=no-name-in-module
from sh import ErrorReturnCode_1  # pylint: disable=no-name-in-module
from sh import which  # pylint: disable=no-name-in-module


docker = functools.partial(_docker,  _out=sys.stdout, _err=sys.stderr)
docker_compose = functools.partial(_docker_compose,  _out=sys.stdout, _err=sys.stderr)


PROJECT_DIR = Path(os.path.dirname(__file__))


def slugify(string):
    return re.sub(r'[^\w]', '-', string).lower()


def configure_qemu():
    if not which('qemu-aarch64'):
        raise RuntimeError(
            'QEMU not installed, install missing package (apt: qemu,qemu-user-static | pacman: qemu-headless,qemu-headless-arch-extra | brew: qemu).')

    images = docker('images', '--format',
                    '{{ .Repository }}', _out=None, _err=None)
    if 'multiarch/qemu-user-static' not in images:
        docker('run', '--rm', '--privileged',
               'multiarch/qemu-user-static', '--reset', '-p', 'yes')


def render(template, out, context):
    with PROJECT_DIR:
        with open(template, 'r') as f:
            rendered = f.read().format(**context)
        dir_name = os.path.dirname(out)
        prev = None
        if not os.path.exists(dir_name):
            os.makedirs(dir_name)
        with open(out, 'w') as f:
            f.write(rendered)
        print(f'Wrote {out}')
        return out


class Distro(metaclass=abc.ABCMeta):
    template_path = None
    registry = {}
    host_archs = ()
    client_archs = ()
    ports_by_arch = {}
    toolchains_by_arch = {}

    def __init__(self, name):
        self.name = name
        self.registry[name] = self

    def __repr__(self):
        return f'{self.__class__.__name__}({repr(self.name)})'

    @classmethod
    def get(cls, name):
        if name not in cls.registry:
            raise ValueError(
                f'Unsupported distro {name}, choose from {", ".join(cls.registry.keys())}')
        return cls.registry[name]

    @classmethod
    def clean_all(cls):
        for distro in cls.registry.values():
            distro.clean()

    def clean(self):
        shutil.rmtree(self.out_path)
        print(f'Removed {self.out_path}')

    @classmethod
    def build_all(cls, tag=None):
        for distro in cls.registry.values():
            for host_arch in distro.host_archs:
                distro.build_host(host_arch, tag=tag)
            for client_arch in distro.client_archs:
                distro.build_client(client_arch, tag=tag)

    @property
    def out_path(self):
        return Path(slugify(self.name))

    @abc.abstractmethod
    def get_compiler_path_part_by_arch(self, host_arch, client_arch):
        ...

    def get_template_context(self, **context):
        context.update(dict(
            distro=self.name,
            distro_slug=slugify(self.name),
        ))

        if not context.get('tag'):
            context['tag'] = 'devel'

        host_arch = context.get('host_arch')
        client_arch = context.get('client_arch')
        if client_arch:
            context.update(dict(
                host_port=self.ports_by_arch[client_arch],
                toolchain=self.toolchains_by_arch.get(client_arch),
            ))
            if host_arch:
                context.update(dict(
                    compiler_path_part=self.get_compiler_path_part_by_arch(
                        host_arch, client_arch),
                ))
        return context

    def render(self, **kwargs):
        self.render_dockerfile_host(**kwargs)
        self.render_dockerfile_client(**kwargs)
        self.render_docker_compose(**kwargs)

    def render_dockerfile_host(self, **kwargs):
        for host_arch in self.host_archs:
            context = self.get_template_context(host_arch=host_arch, **kwargs)
            render(
                self.template_path / 'host/Dockerfile.template',
                self.out_path / 'host' / f'Dockerfile.{host_arch}',
                context,
            )

    def render_dockerfile_client(self, **kwargs):
        for client_arch in self.client_archs:
            context = self.get_template_context(client_arch=client_arch, **kwargs)
            render(
                self.template_path / 'client/Dockerfile.template',
                self.out_path / 'client' /
                f'Dockerfile.{client_arch}',
                context,
            )

    def docker_compose_file_path(self, host_arch, client_arch):
        distro_slug = slugify(self.name)
        return self.out_path / f'docker-compose.{distro_slug}.host-{host_arch}.client-{client_arch}.yml'

    def render_docker_compose(self, **kwargs):
        for host_arch in self.host_archs:
            for client_arch in self.client_archs:
                context = self.get_template_context(
                    host_arch=host_arch, client_arch=client_arch, **kwargs)
                render(
                    self.template_path / 'docker-compose.template.yml',
                    self.docker_compose_file_path(host_arch, client_arch),
                    context,
                )

    def build_host(self, host_arch, **kwargs):
        configure_qemu()

        context = self.get_template_context(**kwargs)
        self.render(**context)

        image = 'elijahru/distcc-cross-compiler-host-{distro_slug}:{tag}-{host_arch}'.format(host_arch=host_arch, **context)
        dockerfile = self.out_path / f'host/Dockerfile.{host_arch}'
        try:
            docker('pull', image)
        except ErrorReturnCode_1:
            pass
        docker(
            'build',
            self.out_path / 'host/build-context',
            '--file', dockerfile,
            '--tag', image,
            '--cache-from', image,
        )

    def build_client(self, client_arch, **kwargs):
        configure_qemu()
        context = self.get_template_context(client_arch=client_arch, **kwargs)
        self.render(**context)

        image = 'elijahru/distcc-cross-compiler-client-{distro_slug}:{tag}-{client_arch}'.format(**context)
        dockerfile = self.out_path / f'client/Dockerfile.{client_arch}'
        try:
            docker('pull', image)
        except ErrorReturnCode_1:
            pass
        docker(
            'build',
            self.out_path / 'client/build-context',
            '--file', dockerfile,
            '--tag', image,
            '--cache-from', image,
        )

    def test(self, host_arch, client_arch, **kwargs):
        self.render(**kwargs)

        docker_compose_file = self.docker_compose_file_path(host_arch, client_arch)

        # Bring up the distccd host
        docker_compose('-f', docker_compose_file, 'up', '-d', 'distcc-cross-compiler-host')
        time.sleep(5)

        docker_compose('-f', docker_compose_file, 'run', 'distcc-cross-compiler-client')


class DebianLike(Distro):
    template_path = Path('debian-like')

    host_archs = ('amd64', 'i386', 'arm32v7', 'arm64v8', 'ppc64le', 's390x')
    client_archs = ('amd64', 'i386', 'arm32v7', 'arm64v8', 'ppc64le', 's390x')
    compilers_by_host_arch = {
        'amd64': ('amd64', 'i386', 'arm32v7', 'arm64v8', 'ppc64le', 's390x'),
        'i386': ('amd64', 'i386', 'arm64v8', 'ppc64le'),
        'arm32v7': ('arm32v7', ),
        'arm64v8': ('amd64', 'i386', 'arm64v8'),
        'ppc64le': ('amd64', 'i386', 'arm64v8', 'ppc64le'),
        's390x': ('s390x', ),
    }
    packages_by_arch = {
        'amd64': 'gcc-x86-64-linux-gnu g++-x86-64-linux-gnu binutils-x86-64-linux-gnu',
        'i386': 'gcc-i686-linux-gnu g++-i686-linux-gnu binutils-i686-linux-gnu',
        'arm32v7': 'gcc-arm-linux-gnueabihf g++-arm-linux-gnueabihf binutils-arm-linux-gnueabihf',
        'arm64v8': 'gcc-aarch64-linux-gnu g++-aarch64-linux-gnu binutils-aarch64-linux-gnu',
        'ppc64le': 'gcc-powerpc64le-linux-gnu g++-powerpc64le-linux-gnu binutils-powerpc64le-linux-gnu',
        's390x': 'gcc-s390x-linux-gnu g++-s390x-linux-gnu binutils-s390x-linux-gnu',
    }
    ports_by_arch = {
        'i386': 3603,
        'amd64': 3604,
        'arm32v7': 3607,
        'arm64v8': 3608,
        's390x': 3609,
        'ppc64le': 3610,
    }
    toolchains_by_arch = {
        'amd64': 'x86_64-linux-gnu',
        'i386': 'i686-linux-gnu',
        'ppc64le': 'powerpc64le-linux-gnu',
        's390x': 's390x-linux-gnu',
        'arm32v7': 'arm-linux-gnueabihf',
        'arm64v8': 'aarch64-linux-gnu',
    }
    flags_by_arch = {
        'amd64': 'START_DISTCC_X86_64_LINUX_GNU',
        'i386': 'START_DISTCC_I686_LINUX_GNU',
        'ppc64le': 'START_DISTCC_PPC64LE_LINUX_GNU',
        's390x': 'START_DISTCC_S390X_LINUX_GNU',
        'arm32v7': 'START_DISTCC_ARM_LINUX_GNUEABIHF',
        'arm64v8': 'START_DISTCC_AARCH64_LINUX_GNU',
    }

    def get_apt_packages_by_host_arch(self, host_arch):
        packages = 'build-essential g++ distcc'
        for compiler_arch in self.compilers_by_host_arch[host_arch]:
            packages += f' {self.packages_by_arch[compiler_arch]}'
        return packages

    def get_compiler_path_part_by_arch(self, host_arch, compiler_arch):
        if host_arch == compiler_arch:
            return ''
        return Path('/usr/local/') / self.toolchains_by_arch[compiler_arch] / 'bin:'

    def get_template_context(self, **context):
        context = super(DebianLike, self).get_template_context(**context)
        client_arch = context.get('client_arch')
        host_arch = context.get('host_arch')

        if client_arch:
            context.update(dict(
                flag=self.flags_by_arch[client_arch],
            ))

        if host_arch:
            context.update(dict(
                packages=self.get_apt_packages_by_host_arch(host_arch),
            ))

        return context

    def render(self, **kwargs):
        super(DebianLike, self).render(**kwargs)
        self.render_initd_distccd(**kwargs)

    def render_initd_distccd(self, **kwargs):
        for host_arch in self.host_archs:
            for client_arch in self.compilers_by_host_arch[host_arch]:
                context = self.get_template_context(
                    host_arch=host_arch, client_arch=client_arch, **kwargs)

                render(
                    self.template_path /
                    'host/build-context/etc/default/distccd.template',
                    self.out_path / 'host/build-context/etc/default' /
                    f'distccd.host-{host_arch}.client-{client_arch}',
                    context,
                )
                render(
                    self.template_path /
                    'host/build-context/etc/init.d/distccd.template',
                    self.out_path / 'host/build-context/etc/init.d' /
                    f'distccd.host-{host_arch}.client-{client_arch}',
                    context,
                )
                render(
                    self.template_path /
                    'host/build-context/etc/logrotate.d/distccd.template',
                    self.out_path / 'host/build-context/etc/logrotate.d' /
                    f'distccd.host-{host_arch}.client-{client_arch}',
                    context,
                )


class ArchLinuxLike(Distro):
    template_path = Path('archlinux-like')

    host_archs = ('amd64', )
    client_archs = ('amd64', 'arm32v5', 'arm32v6', 'arm32v7', 'arm64v8', )
    compilers_by_host_arch = {
        'amd64': ('amd64', 'arm32v5', 'arm32v6', 'arm32v7', 'arm64v8', ),
    }
    ports_by_arch = {
        'amd64': 3704,
        'arm32v5': 3705,
        'arm32v6': 3706,
        'arm32v7': 3707,
        'arm64v8': 3708,
    }
    images_by_arch = {
        'amd64': 'archlinux:20200908',
        'arm32v5': 'lopsided/archlinux@sha256:66b26a83a39e26e2a390b5b92105f80e6042d0db79ee22b1f57d169307b87a58',
        'arm32v6': 'lopsided/archlinux@sha256:109729d4d863e14fed6faa1437f0eaee8133b26c310079c8294a4c7db6dbebb5',
        'arm32v7': 'lopsided/archlinux@sha256:fbf2d806f207a2e9a5400bd20672b80ca318a2e59fc56c1c0f90b4e9adb60f4a',
        'arm64v8': 'lopsided/archlinux@sha256:f9d68dd73a85b587539e04ef26b18d91b243bee8e1a343ad97f67183f275e548'
    }
    toolchains_by_arch = {
        'arm32v5': '/toolchains/x-tools/arm-unknown-linux-gnueabi',
        'arm32v6': '/toolchains/x-tools6h/arm-unknown-linux-gnueabihf',
        'arm32v7': '/toolchains/x-tools7h/arm-unknown-linux-gnueabihf',
        'arm64v8': '/toolchains/x-tools8/aarch64-unknown-linux-gnu',
    }

    def get_compiler_path_part_by_arch(self, host_arch, client_arch):
        if host_arch == client_arch:
            return ''
        return Path(self.toolchains_by_arch[client_arch]) / 'bin:'

    def get_template_context(self, **context):
        context = super(ArchLinuxLike, self).get_template_context(**context)
        host_arch = context.get('host_arch')
        client_arch = context.get('client_arch')
        if client_arch:
            context.update(dict(
                client_image=self.images_by_arch[client_arch],
                toolchain=self.toolchains_by_arch.get(client_arch),
            ))
        if host_arch:
            context.update(dict(
                host_image=self.images_by_arch[host_arch],
            ))
        return context

    def render(self, **kwargs):
        super(ArchLinuxLike, self).render(**kwargs)
        self.render_systemd_distccd(**kwargs)

    def render_systemd_distccd(self, **kwargs):
        for host_arch in self.host_archs:
            for client_arch in self.compilers_by_host_arch[host_arch]:
                context = self.get_template_context(
                    host_arch=host_arch, client_arch=client_arch, **kwargs)

                render(
                    self.template_path /
                    'host/build-context/etc/conf.d/distccd.template',
                    self.out_path / 'host/build-context/etc/conf.d' /
                    f'distccd.host-{host_arch}.client-{client_arch}',
                    context,
                )
                render(
                    self.template_path /
                    'host/build-context/usr/lib/systemd/system/distccd.template.service',
                    self.out_path / 'host/build-context/usr/lib/systemd/system' /
                    f'distccd.host-{host_arch}.client-{client_arch}.service',
                    context,
                )

        host_build_context_dir = self.out_path / 'host/build-context'
        client_build_context_dir = self.out_path / 'client/build-context'

        with PROJECT_DIR:
            scripts = self.template_path / 'client/scripts'
            shutil.copytree(scripts, client_build_context_dir / 'scripts')
            print(f'Copied {scripts} to {client_build_context_dir}')

            shared = Path('shared-build-context/client')
            shutil.copytree(shared, client_build_context_dir)
            print(f'Copied {shared} to {client_build_context_dir}')

            scripts = self.template_path / 'host/scripts'
            shutil.copytree(scripts, host_build_context_dir / 'scripts')
            print(f'Copied {scripts} to {host_build_context_dir}')

            shared = Path('shared-build-context/host')
            shutil.copytree(shared, host_build_context_dir)
            print(f'Copied {shared} to {host_build_context_dir}')



# Register supported distributions
debian_buster = DebianLike('debian:buster')
archlinux = ArchLinuxLike('archlinux')


def make_parser():
    parser = argparse.ArgumentParser()

    subparsers = parser.add_subparsers(dest='subcommand')

    # list-distros
    subparsers.add_parser('list-distros')

    # list-host-archs
    parser_list_host_archs = subparsers.add_parser('list-host-archs')
    parser_list_host_archs.add_argument('distro', type=Distro.get)

    # list-client-archs
    parser_list_client_archs = subparsers.add_parser('list-client-archs')
    parser_list_client_archs.add_argument('distro', type=Distro.get)

    # render
    parser_render = subparsers.add_parser('render')
    parser_render.add_argument('distro', type=Distro.get)
    parser_render.add_argument('--tag', default='devel')

    # render-all
    parser_render_all = subparsers.add_parser('render-all')
    parser_render_all.add_argument('--tag', default='devel')

    # build-host
    parser_build_host = subparsers.add_parser('build-host')
    parser_build_host.add_argument('distro', type=Distro.get)
    parser_build_host.add_argument('arch')
    parser_build_host.add_argument('--tag', default='devel')

    # build-client
    parser_build_client = subparsers.add_parser('build-client')
    parser_build_client.add_argument('distro', type=Distro.get)
    parser_build_client.add_argument('arch')
    parser_build_client.add_argument('--tag', default='devel')

    # build-all
    parser_build_all = subparsers.add_parser('build-all')
    parser_build_all.add_argument('--tag', default='devel')

    # clean
    subparsers.add_parser('clean')

    # test
    parser_test = subparsers.add_parser('test')
    parser_test.add_argument('distro', type=Distro.get)
    parser_test.add_argument('host_arch')
    parser_test.add_argument('client_arch')
    parser_test.add_argument('--tag', default='devel')

    return parser


def main():
    args = make_parser().parse_args()
    if args.subcommand == 'list-distros':
        print('\n'.join(Distro.registry.keys()))

    elif args.subcommand == 'list-host-archs':
        print('\n'.join(args.distro.host_archs))

    elif args.subcommand == 'list-client-archs':
        print('\n'.join(args.distro.client_archs))

    elif args.subcommand == 'render':
        args.distro.render(tag=args.tag)

    elif args.subcommand == 'render-all':
        for distro in Distro.registry.values():
            distro.render(tag=args.tag)

    elif args.subcommand == 'build-host':
        args.distro.build_host(host_arch=args.arch, tag=args.tag)

    elif args.subcommand == 'build-client':
        args.distro.build_client(client_arch=args.arch, tag=args.tag)

    elif args.subcommand == 'build-all':
        Distro.build_all(tag=args.tag)

    elif args.subcommand == 'clean':
        Distro.clean_all()

    elif args.subcommand == 'test':
        args.distro.test(host_arch=args.host_arch, client_arch=args.client_arch, tag=args.tag)


if __name__ == '__main__':
    main()
