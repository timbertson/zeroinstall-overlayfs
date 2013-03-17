#!/usr/bin/env python
import sys, os
import urllib2
from debian import debian_support
import subprocess
import re
import gzip
import hashlib
import shutil
import contextlib
import itertools
import tempfile
import logging
import make_overlay
LOGGER = logging.getLogger(__name__)

CACHE_DIR = 'debcache'
if not os.path.exists(CACHE_DIR):
	os.makedirs(CACHE_DIR)

class VersionRestriction(object):
	LT = "<<"
	LTE = "<="
	EQ = "="
	GTE = ">="
	GT = ">>"
	OPS = (LT, LTE, EQ, GTE, GT, None)
	def __init__(self, op, version):
		assert op in self.OPS, "Invalid operator: %s" % (op,)
		self.op = op
		self.version = version
	def zi_xml(self):
		import cgi
		version = cgi.escape(self.version)
		prev = version + "-pre"
		next = version + "-post"
		xml = "<version "
		if self.opt is None:
			pass
		elif self.op == self.LT:
			xml += 'before=\"%s\"' % version
		elif self.op == self.LTE:
			xml += 'before=\"%s\"' % next
		elif self.op == self.EQ:
			xml += 'before=\"%s\" ' % next
			xml += 'not-before=\"%s\" ' % prev
		elif self.op == self.GTE:
			xml += 'not-before=\"%s\"' % version
		elif self.op == self.GT:
			xml += 'not-before=\"%s\"' % next
		else:
			assert False
		xml += "/>"
		return xml
	def __repr__(self):
		return "Version: %s %s" % (self.op, self.version)


def rdepends(pkid, package_map, exclude=[]):
	all_deps = set()
	new_deps = set([pkid])
	seen_excludes = set()
	while new_deps:
		current_deps = new_deps
		new_deps = set()
		all_deps.update(current_deps)
		
		for pkg_id in current_deps:
			if pkg_id not in package_map:
				LOGGER.warn("could not find package info for %s" % (pkg_id,))
				continue
			dep_def = package_map[pkg_id].get('Depends', None)
			LOGGER.debug("%s depends on: %s" % (pkg_id, dep_def))
			if dep_def is None: continue
			for dep in parse_depends(dep_def):
				id, ver = dep

				if id in exclude:
					if id not in seen_excludes:
						seen_excludes.add(id)
						LOGGER.info("Skipping excluded package: %s", id)
					continue
				
				if id in all_deps:
					LOGGER.debug("(skipping duplicate %s)" % (id,))
					continue
				LOGGER.debug("Adding %s" % (id,))
				new_deps.add(id)
	return all_deps


def parse_depends(s):
	groups = s.split("|")
	if len(groups) > 1:
		LOGGER.warn("Depends string has %s groups! Using first: %s", len(groups), s)
	
	items = groups[0].split(", ")
	deps = []
	extractor = re.compile("^ *(?P<id>[^ ]+) *(\[(?P<arch>[^]]+)\])? *(\((?P<op>[=<>]+) +(?P<version>[^)]+)\))? *$")
	for item in items:
		try:
			groups = extractor.match(item).groupdict()
		except AttributeError:
			raise ValueEror("Invalid depend item: %s" % (item,))
		LOGGER.debug(repr(groups))
		#TODO: arch...
		deps.append((groups['id'], VersionRestriction(groups['op'], groups['version'])))
	return deps
		


def download_packages_file(url):
	LOGGER.info("Downloading: %s" % url,)
	packages_filename = os.path.join(CACHE_DIR, "Packages-%s" % (hashlib.md5(url).hexdigest()[:10]))
	if os.path.exists(packages_filename):
		LOGGER.info("Using cached %s" % (packages_filename))
	else:
		try:
			req = urllib2.urlopen(url)
			with tempfile.NamedTemporaryFile() as tmp:
				LOGGER.info("tempfile: %s", tmp.name)
				with contextlib.closing(req):
					shutil.copyfileobj(req, tmp)
				tmp.seek(0)
				gz = gzip.GzipFile(fileobj=tmp, mode='rb')
				with contextlib.closing(gz):
					with open(packages_filename, "w") as packages_file:
						shutil.copyfileobj(gz, packages_file)
		except:
			if os.path.exists(packages_filename):
				os.remove(packages_filename)
			raise
	return packages_filename

class RepositorySource(object):
	def __init__(self, base, distribution, components, arches):
		self.base = base
		self.distribution = distribution
		self.components = components
		self.arches = arches
	
	@property
	def repositories(self):
		for (component, arch) in itertools.product(self.components, self.arches):
			yield Repository(self, component, arch)
	
class Repository(object):
	def __init__(self, repository, component, arch):
		self.repository = repository
		self.component = component
		self.arch_type = "source" if arch == "source" else "binary"
		self.arch = arch

	@property
	def packages_urls(self):
		yield "{self.repository.base}/dists/{self.repository.distribution}/{self.component}/{self.arch_type}-{self.arch}/Packages.gz".format(**locals())
	
	def deb_url(self, package_id, package_info):
		version = package_info['Version']
		source = package_info.get('Source', package_id).split(" ",1)[0]
		if ":" in version:
			# strip epoch, if present
			version = version.split(":",1)[-1]
		if source.startswith("lib"):
			letter = source[:4]
		else:
			letter = source[:1]
		LOGGER.debug("-- LOCALS:")
		for k, v in locals().items():
			LOGGER.debug("  %s = %r", k,v)
		arch = package_info.get('Architecture', self.arch)
		return "{self.repository.base}/pool/{self.component}/{letter}/{source}/{package_id}_{version}_{arch}.deb".format(**locals())
	
	@property
	def packages(self):
		for packages_url in self.packages_urls:
			packages_file = download_packages_file(packages_url)
			packagefile = debian_support.PackageFile(packages_file)
			for package in packagefile:
				pd = dict(package)
				id = pd.pop("Package")
				pd['repo'] = self
				yield (id, pd)

class PackageCache(object):
	def __init__(self, repository_sources):
		self.sources = repository_sources
	
	@property
	def packages(self):
		d = {}
		for source in self.sources:
			for repo in source.repositories:
				for id, package_info in repo.packages:
					if id in d:
						LOGGER.warn("duplicate package found: %s" % (id,))
						continue
					d[id] = package_info
		return d

def download_all(name, package_map, exclude=[]):
	dest = os.path.join(CACHE_DIR, "group-%s" % name)
	deb_dest = os.path.join(CACHE_DIR, "debs")
	unpacked_deb_dest = os.path.join(CACHE_DIR, "debs-unpacked")
	
	unpacked_paths = []
	for d in (dest, deb_dest, unpacked_deb_dest):
		if not os.path.exists(d): os.makedirs(d)
	
	for package_id in list(rdepends(name, package_map, exclude=exclude)):
		LOGGER.info("Processing %s", package_id)
		try:
			package = package_map[package_id]
		except KeyError:
			LOGGER.warn("Skipping unknown package: %s", package_id)
			continue
		
		url = package['repo'].deb_url(package_id, package)
		deb_file_loc = os.path.join(deb_dest, url.rsplit("/", 1)[-1])
		unpacked = os.path.join(unpacked_deb_dest, package_id)

		if not os.path.exists(deb_file_loc):
			LOGGER.info("Downloading deb: %s -> %s", url, deb_file_loc)
			with contextlib.closing(urllib2.urlopen(url)) as req:
				with open(deb_file_loc, 'w') as out:
					shutil.copyfileobj(req, out)
			if os.path.exists(unpacked):
				shutil.rmtree(unpacked)

		if not os.path.exists(unpacked):
			os.makedirs(unpacked)
			LOGGER.info("Unpacking deb: %s -> %s", deb_file_loc, unpacked)
			try:
				from zeroinstall.zerostore.unpack import extract_deb
				with open(deb_file_loc) as deb_file:
					extract_deb(deb_file, unpacked)
			except:
				shutil.rmtree(unpacked)
				raise

		unpacked_paths.append(unpacked)
	return unpacked_paths

def main():
	import optparse
	p = optparse.OptionParser()
	p.add_option("-p", "--package")
	p.add_option("--print-deps", action='store_true')
	p.add_option("-v", "--verbose", action="store_true")
	opts, cmd = p.parse_args()
	assert opts.package, "Must provide a package"
	level = logging.DEBUG if opts.verbose else logging.INFO
	logging.basicConfig(level=level)
	LOGGER.setLevel(level)

	SOURCES = [
		RepositorySource("http://au.archive.ubuntu.com/ubuntu", "quantal", ["main"], ["amd64"]),
	]

	PACKAGE_MAP=PackageCache(SOURCES).packages

	if opts.print_deps:
		for dep in sorted(rdepends(opts.package, PACKAGE_MAP)):
			print " - %s" % (dep,)
		return

	roots = download_all(opts.package, PACKAGE_MAP, exclude=["libc6"])
	roots = list(map(os.path.abspath, roots))

	tempdir = tempfile.mkdtemp()
	LOGGER.info("making chroot in: %s", tempdir)
	with make_overlay.overlayfs(chroot = tempdir, overlay_roots = roots, sacred_paths = ['/home', '/tmp'], prefer_existing_files=['/etc']):
		cmd = ["proot", "-r", tempdir, "--bind=/:/" + make_overlay.ROOT_FOLDER_NAME] + cmd
		print "running cmd: %r" % (cmd,)
		try:
			subprocess.check_call(cmd)
		finally:
			print "Command exited. Press return to continue cleanup (tempdir = %s)" % (tempdir,)
			raw_input()

if __name__ == '__main__':
	main()

