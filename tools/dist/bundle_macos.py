#!/usr/bin/env python3
#
# bundle_macos.py -- Make Subversion distribution self-contained on macOS
#

import os
import sys
import shutil
import subprocess
from pathlib import Path

def run_cmd(cmd, check=True):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check and p.returncode != 0:
        print(f"Error running {' '.join(cmd)}:\n{p.stderr}", file=sys.stderr)
        sys.exit(p.returncode)
    return p

def get_dependencies(macho_path):
    p = subprocess.run(['otool', '-L', str(macho_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        return []
    lines = p.stdout.strip().splitlines()
    deps = []
    # line 0 is the macho_path itself
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        dep = line.split(' ')[0]
        deps.append(dep)
    return deps

def should_bundle(dep):
    # System libraries that must be left alone
    if dep.startswith(('/usr/lib/', '/System/Library/', '/Library/Frameworks/')):
        return False
    # If already using @rpath or @executable_path, it's already relocatable
    if dep.startswith(('@rpath/', '@executable_path/', '@loader_path/')):
        return False
    return True

def add_rpath(macho_path, rpath):
    p = subprocess.run(['otool', '-l', str(macho_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if f"path {rpath}" in p.stdout:
        return
    subprocess.run(['install_name_tool', '-add_rpath', rpath, str(macho_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def bundle(dist_dir):
    dist_dir = Path(dist_dir).resolve()
    bin_dir = dist_dir / "bin"
    lib_dir = dist_dir / "lib"
    lib_dir.mkdir(parents=True, exist_ok=True)

    print(f"==> Bundling dependencies in: {dist_dir}")
    print(f"    bin: {bin_dir}")
    print(f"    lib: {lib_dir}")

    processed = set()
    to_process = set()

    # Collect initial binaries and existing dylibs
    if bin_dir.exists():
        for f in bin_dir.iterdir():
            if f.is_file() and not f.is_symlink():
                to_process.add(f.resolve())

    if lib_dir.exists():
        for f in lib_dir.iterdir():
            if f.is_file() and not f.is_symlink() and (f.suffix == '.dylib' or '.dylib.' in f.name):
                to_process.add(f.resolve())

    while to_process:
        target = to_process.pop()
        if target in processed:
            continue
        processed.add(target)

        deps = get_dependencies(target)
        for dep in deps:
            if not should_bundle(dep):
                continue

            orig_dep_path = Path(dep)
            real_dep_path = orig_dep_path.resolve()

            if not real_dep_path.exists():
                print(f"WARNING: Dependency not found on disk: {dep} (referenced by {target.name})")
                continue

            # Target filename inside lib_dir
            dest_name = real_dep_path.name
            dest_file = lib_dir / dest_name

            # If not yet copied, copy it
            if not dest_file.exists():
                print(f"--> Copying dependency: {real_dep_path} -> {dest_file}")
                shutil.copy2(real_dep_path, dest_file)
                dest_file.chmod(0o755)
                # Change ID of newly copied library to @rpath/dest_name
                run_cmd(['install_name_tool', '-id', f"@rpath/{dest_name}", str(dest_file)], check=False)
                # Also add @loader_path rpath to the copied dylib
                add_rpath(dest_file, "@loader_path")
                # Add to queue so its dependencies will also be processed
                to_process.add(dest_file)

            # Also create symlink if orig_dep_path.name != dest_name
            if orig_dep_path.name != dest_name:
                alias = lib_dir / orig_dep_path.name
                if not alias.exists():
                    alias.symlink_to(dest_name)

            # Rewrite reference in target from `dep` to `@rpath/{orig_dep_path.name}`
            new_ref = f"@rpath/{orig_dep_path.name}"
            run_cmd(['install_name_tool', '-change', dep, new_ref, str(target)], check=False)

    # Second pass: ensure all dylibs in lib_dir reference each other via @rpath
    for dylib in lib_dir.iterdir():
        if dylib.is_file() and not dylib.is_symlink():
            run_cmd(['install_name_tool', '-id', f"@rpath/{dylib.name}", str(dylib)], check=False)
            add_rpath(dylib, "@loader_path")
            for dep in get_dependencies(dylib):
                dep_path = Path(dep)
                bundled_match = lib_dir / dep_path.name
                if bundled_match.exists() and should_bundle(dep):
                    run_cmd(['install_name_tool', '-change', dep, f"@rpath/{dep_path.name}", str(dylib)], check=False)

    # Set rpath on all binaries
    if bin_dir.exists():
        for exe in bin_dir.iterdir():
            if exe.is_file() and not exe.is_symlink():
                add_rpath(exe, "@executable_path/../lib")

    # Code-sign all dylibs and binaries
    print("==> Ad-hoc code signing bundled libraries and binaries...")
    for dylib in lib_dir.iterdir():
        if dylib.is_file() and not dylib.is_symlink():
            run_cmd(['codesign', '--force', '--sign', '-', str(dylib)], check=False)

    if bin_dir.exists():
        for exe in bin_dir.iterdir():
            if exe.is_file() and not exe.is_symlink():
                run_cmd(['codesign', '--force', '--sign', '-', str(exe)], check=False)

    print("==> Bundling completed successfully!")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <dist_dir>")
        sys.exit(1)
    bundle(sys.argv[1])
