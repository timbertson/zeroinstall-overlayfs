#!/usr/bin/env python
from __future__ import print_function
import os,sys
import subprocess
from optparse import OptionParser
import logging
import shutil
import contextlib
from collections import defaultdict

SYSTEM_MOUNTPOINTS = frozenset(['proc', 'sys', 'var'])
LOGGER = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
DRY_RUN = False
CHECK = False
ROOT_FOLDER_NAME = '__root'

def main():
	global DRY_RUN, CHECK
	p = OptionParser(usage="%prog [OPTIONS] cmd")
	p.add_option('-b', '--base')
	p.add_option('-c', '--check', default=False, action='store_true')
	p.add_option('-v','--verbose', action='store_true')
	p.add_option('--protect', action='append', default=[], dest='shadow_dirs')
	p.add_option('-n', '--dry-run', action='store_true', help='print commands, but do nothing')
	p.add_option('--never-overlay', action='append', default=[])
	p.add_option('--prefer-existing', action='append', default=[])

	opts,cmd = p.parse_args()
	assert opts.base, "Please provide a base directory"
	if opts.verbose:
		LOGGER.setLevel(logging.DEBUG)
	CHECK = opts.check

	DRY_RUN = opts.dry_run

	#TODO: remove duplicates while maintaining order
	overlay_roots = [path for path in os.environ['OVERLAY_ROOTS'].split(os.pathsep) if path]

	assert "/" not in overlay_roots
	with overlayfs(opts.base, overlay_roots, sacred_paths = opts.never_overlay, prefer_existing_files = opts.prefer_existing):
		if not cmd:
			print("Overlay complete. Press return to end...")
			raw_input()
		else:
			print("running cmd: %r" % (cmd,))
			subprocess.check_call(["proot", "-r", opts.base, "--bind=/:/" + ROOT_FOLDER_NAME, "--"] + cmd)

def action(fn, *a):
	LOGGER.debug("calling %s(%s)", fn.__name__, ",".join(map(repr, a)))
	if DRY_RUN:
		print("would call %s(%s)" % (fn.__name__, ",".join(map(repr, a))))
		return False
	if CHECK:
		print("going to call %s(%s) - OK?" % (fn.__name__, ",".join(map(repr, a))))
		raw_input()
	return fn(*a)

@contextlib.contextmanager
def overlayfs(chroot, overlay_roots, sacred_paths=[], prefer_existing_files=[]):

	# turn into relative paths (from root)
	def relative_folder_path(p):
		assert os.path.isabs(p)
		assert os.path.isdir(p)
		return os.path.join(p.lstrip("/"), "")

	sacred_paths = list(map(relative_folder_path, sacred_paths))
	LOGGER.debug("SACRED_PATHS: %r", sacred_paths)
	prefer_existing_files = list(map(relative_folder_path, prefer_existing_files))
	LOGGER.debug("PREFER_EXISTING_FILES: %r", prefer_existing_files)

	LOGGER.debug("overlay roots: %r" % (overlay_roots,))
	root_folder_name, mounts = init_chroot(chroot)
	LOGGER.debug("MOUNTS: %r",mounts)
	root_path = os.path.join(chroot, root_folder_name)

	ensure_dir(root_path)
	try:
		apply_overlay_mapping(chroot, root_folder_name, overlay_roots, sacred_paths, prefer_existing_files)
		LOGGER.debug("MOUNTS: %r",mounts)
		yield
	except:
		# import pdb; pdb.set_trace()
		raise
	finally:
		print("Cleaning up...")
		# need_to_umount = [os.path.join(root_path, mount.lstrip("/")) for mount in mounts]
		# try:
		# 	while need_to_umount:
		# 		umount(need_to_umount.pop(0))
		erase(chroot)
		# except:
		# 	print("Cleanup failed. You MUST unmount the following paths:%s\n\nand then remove %s" % ("".join(["\n - " + mount for mount in need_to_umount]), chroot))
		# 	raise
	print("Done.")

def ensure_dir(dest):
	if not os.path.lexists(dest):
		action(os.makedirs, dest)

def bind_mount(src, mountpoint):
	mountpoint = mountpoint.rstrip("/")
	#TODO: (if using something like lxc / user namespaces)
	# assert os.listdir(mountpoint) == []
	# action(subprocess.check_call, ['mount','--bind', src, mountpoint])

	# for proot, the following works fine:
	ensure_dir(os.path.dirname(mountpoint))
	assert not os.path.lexists(mountpoint)
	action(subprocess.check_call, ['ln','-s', src, mountpoint])

def umount(dest):
	#TODO: action(subprocess.check_call, ['umount', dest])
	action(subprocess.check_call, ['rm', dest.rstrip("/")])

def erase(dest):
	action(shutil.rmtree, dest)

def init_chroot(chroot):
	assert (not os.path.lexists(chroot)) or os.listdir(chroot) == [], "Chroot exists (and is not empty!)"
	root_folder_name = ROOT_FOLDER_NAME
	mounts = [line.split()[2] for line in subprocess.check_output(["mount"]).strip().splitlines()]
	mounts = sorted(mounts, key=len)

	def should_mount(mount):
		mount_relpath = mount.lstrip("/")
		if mount_relpath in SYSTEM_MOUNTPOINTS or any([mount_relpath.startswith(sys_path + "/") for sys_path in SYSTEM_MOUNTPOINTS]):
			LOGGER.info("Skipping system mountpoint: %s", mount)
			return False
		return True

	mounts = list(filter(should_mount, mounts))
	# for mount in mounts[:]:
	# 	mount_relpath = mount.lstrip("/")
	# 	assert not os.path.isabs(mount_relpath)
	# 	if mount_relpath in SYSTEM_MOUNTPOINTS or any([mount_relpath.startswith(sys_path + "/") for sys_path in SYSTEM_MOUNTPOINTS]):
	# 		LOGGER.info("Skipping system mountpoint: %s", mount)
	# 		continue
	# 	try:
	# 		bind_mount(mount, os.path.join(chroot, os.path.join(root_folder_name, mount_relpath)))
	# 	except (IOError, OSError, AssertionError) as e:
	# 		LOGGER.warn("FAILED bind mount on %s - %s: %s", mount, type(e).__name__, e)
	# 		mounts.remove(mount)
	return root_folder_name, mounts

def is_prefix_of(a,b):
	a = os.path.normpath(os.path.join("/",a)) + "/"
	b = os.path.normpath(os.path.join("/",b)) + "/"
	return b.startswith(a)

def apply_overlay_mapping(chroot, root_folder_name, overlay_roots, sacred_paths, prefer_existing_files):
	ROOT = "/"
	def try_place(source, relpath):
		# if relpath in SYSTEM_MOUNTPOINTS:
		# 	LOGGER.debug("skipping system mount (%s, from %s)", relpath, source)
		# 	return True
		assert not os.path.isabs(relpath)
		link_path = os.path.join(chroot, relpath)
		link_dest = os.path.normpath("/%s/%s" % (root_folder_name, os.path.join(source, relpath)))

		parent = os.path.dirname(link_path)
		if not os.path.lexists(parent):
			action(os.makedirs, parent)
		if os.path.lexists(link_path):
			LOGGER.debug("%s already exists - skipping", link_path)
			return False
		action(os.symlink, link_dest, link_path)
		return True

	for root in overlay_roots:
		assert os.path.isabs(root), "Root %s is relative path." % (root,)
		# assert try_place(root, root.lstrip("/"))
	
	current_parents = [(root, '') for root in overlay_roots + [ROOT]]
	while len(current_parents) > 0:
		LOGGER.debug("Beginning loop with current_parents = %r", current_parents)
		next_parents = []
		current_children = defaultdict(lambda: [])

		# for each source & relpath, add child paths
		for source, relpath in current_parents:
			fullpath = os.path.join(source, relpath)
			assert os.path.isdir(fullpath), "Not a directory: %s" % (fullpath,)

			try:
				children = os.listdir(fullpath)
			except (IOError, OSError) as e:
				LOGGER.warn("Can't listdir(%s) - %s: %s" % (fullpath, type(e).__name__, e))
				continue
			for filename in children:
				child_relpath = os.path.join(relpath, filename)
				current_children[child_relpath].append(source)

		LOGGER.debug("current_children = %r", current_children)
		# for each childpath, either link it or add to next_parents for later processing
		for relpath, sources in current_children.items():
			assert len(sources) > 0
			# LOGGER.debug("path %s exists in %s locations (%s)", relpath, len(sources), ", ".join(sources))
			source = sources[0]

			if relpath in sacred_paths:
				# always use the root source for this path, regardless of overlay contents
				LOGGER.debug("path %s is sacred - using ROOT", relpath)
				assert(try_place, ROOT, relpath)
				continue

			if len(sources) == 1:
				LOGGER.debug("found singular: %s (in %s)", relpath, source)
				if try_place(source, relpath):
					continue

			if any([relpath.startswith(base) for base in prefer_existing_files]) and os.path.isfile(os.path.join(ROOT, relpath)):
				LOGGER.debug("Preferring root file for %s", relpath)
				assert try_place(ROOT, relpath)
				continue

			if not os.path.isdir(os.path.join(source, relpath)):
				# if the first preference is a file, just place it:
				LOGGER.debug("found file: %s (in %s)", relpath, source)
				assert try_place(source, relpath)
				continue

			# otherwise, queue children for processing next loop
			for source in sources:
				fullpath = os.path.join(source, relpath)
				if not os.path.isdir(fullpath):
					LOGGER.warn("Skipping non-dir %s (from %s)", relpath, source)
					continue
				LOGGER.debug("queueing %s (under %s)", relpath, source)
				next_parents.append((source, relpath))
		current_parents = next_parents
	LOGGER.info("overlay complete")



def in_subprocess(func):
	"""a helper to run a function in a subprocess"""
	child_pid = os.fork()
	if child_pid == 0:
		os._exit(func() or 0)
	else:
		(pid, status) = os.waitpid(child_pid, 0)
		status = os.WEXITSTATUS(status)
		return status

def execute_as_user(func, user=None):
	def action():
		become_user(user)
		return func()
	return in_subprocess(action)

import pwd
def become_user(name=None):
	if name is None:
		name = os.environ['SUDO_USER']
		assert name != 'root'

	# Get the uid/gid from the name
	running_uid = pwd.getpwnam(name).pw_uid
	running_gid = pwd.getpwnam(name).pw_gid

	# Remove group privileges
	os.setgroups([])

	# Try setting the new uid/gid
	os.setgid(running_gid)
	os.setuid(running_uid)

if __name__ == '__main__':
	main()

