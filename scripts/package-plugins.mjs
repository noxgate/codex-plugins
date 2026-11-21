import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { chmod, lstat, mkdir, mkdtemp, readFile, rename, rm, writeFile } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { zip } from './zip-archive.mjs';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const versionPattern = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/;
const idPattern = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const targetPlatforms = ['macos', 'linux', 'windows'].flatMap((os) => [os + '-arm64', os + '-x64']);
const exec = promisify(execFile);

function allowed(name) {
  return (
    /^(?:plugin\.json|VERSION|CHANGELOG\.md|bin\/(?:noxgate(?:\.sh|\.cmd|\.ps1)?|install\.py)|lib\/(?:noxgate_(?:auth|bootstrap|credentials|models|runtime|recovery)\.py|runtime\.lock)|skills\/[a-z0-9-]+\/(?:SKILL\.md|requirements\.txt|agents\/openai\.yaml|references\/[\w-]+\.md|scripts\/[\w-]+\.py))$/.test(
      name
    ) && !name.includes('/test_')
  );
}

async function safeRead(root, name) {
  let path = root;
  for (const segment of name.split('/')) {
    if (!segment || segment === '.' || segment === '..')
      throw new Error(`Unsafe package path: ${name}`);
    path = join(path, segment);
    if ((await lstat(path)).isSymbolicLink()) throw new Error(`Symlink in plugin: ${name}`);
  }
  if (!(await lstat(path)).isFile()) throw new Error(`Not a file: ${name}`);
  return readFile(path);
}

async function runtimeEntries(sourceRoot, platform, cache) {
  const lock = await safeRead(sourceRoot, 'shared/runtime.lock');
  const assets = lock.toString('utf8').split(/\r?\n/)
    .filter((line) => line && !line.startsWith('#'))
    .map((line) => line.split('\t'))
    .filter(([target]) => target === platform || target === 'all');

  await mkdir(cache, { recursive: true });
  return Promise.all(assets.map(async ([, group, name, sha256, url]) => {
    if (!/^[\w.+-]+\.(?:tar\.gz|whl)$/.test(name) || !/^[a-f0-9]{64}$/.test(sha256) ||
        !['https://github.com', 'https://files.pythonhosted.org'].includes(new URL(url).origin))
      throw new Error(`Invalid runtime entry: ${name}`);
    const matches = (data) => createHash('sha256').update(data).digest('hex') === sha256;
    const path = join(cache, name);
    let data = await readFile(path).catch((error) => {
      if (error.code !== 'ENOENT') throw error;
      return null;
    });
    if (!data || !matches(data)) {
      const stage = await mkdtemp(join(cache, '.download-'));
      try {
        const download = join(stage, name);
        await exec('curl', ['--fail', '--silent', '--show-error', '--location', '--proto', '=https',
          '--proto-redir', '=https', '--retry', '2', '--connect-timeout', '15', '--max-time', '180',
          '--output', download, url]);
        data = await readFile(download);
        if (!matches(data)) throw new Error(`Runtime SHA-256 mismatch: ${name}`);
        await rename(download, path);
      } finally {
        await rm(stage, { recursive: true, force: true });
      }
    }
    return { name: 'runtime/' + name, data, stored: true, group };
  }));
}

export async function packagePlugins({
  root = repoRoot,
  outputRoot = join(root, 'dist'),
  syncShared = false,
  platform = null
} = {}) {
  if (platform && !targetPlatforms.includes(platform)) throw new Error(`Unsupported platform: ${platform}`);
  const sourceRoot = join(root, 'plugins');
  const catalog = JSON.parse(await readFile(join(sourceRoot, 'catalog.json'), 'utf8'));
  if (
    catalog.schemaVersion !== 1 ||
    !Array.isArray(catalog.plugins) ||
    !catalog.plugins.length ||
    !versionPattern.test(catalog.marketplaceVersion)
  )
    throw new Error('Invalid plugin catalog');
  const runtime = platform
    ? await runtimeEntries(sourceRoot, platform, join(root, 'dist/.runtime-cache'))
    : [];
  if (platform && (runtime.filter(({ group }) => group === 'python').length !== 1 ||
                   runtime.filter(({ group }) => group === 'game').length !== 2))
    throw new Error(`Incomplete runtime for ${platform}`);
  const ids = new Set();
  const packages = [];
  for (const spec of catalog.plugins.slice().sort((a, b) => a.id.localeCompare(b.id))) {
    if (!idPattern.test(spec.id) || ids.has(spec.id) || !Array.isArray(spec.files))
      throw new Error(`Invalid plugin id/catalog: ${spec.id}`);
    ids.add(spec.id);
    const pluginRoot = join(sourceRoot, spec.id);
    if ((await lstat(pluginRoot)).isSymbolicLink()) throw new Error(`Symlink in plugin: ${spec.id}`);
    const entries = [];
    for (const name of [...new Set(spec.files)].sort()) {
      if (!allowed(name)) throw new Error(`Disallowed plugin file: ${spec.id}/${name}`);
      entries.push({ name, data: await safeRead(pluginRoot, name) });
    }
    const get = (name) => {
      const entry = entries.find((entry) => entry.name === name);
      if (!entry) throw new Error(`Missing ${spec.id}/${name}`);
      return entry.data.toString('utf8');
    };
    const plugin = JSON.parse(get('plugin.json'));
    const version = get('VERSION').trim();
    const changelog = get('CHANGELOG.md').trim();
    if (
      !versionPattern.test(version) ||
      version !== plugin.version ||
      plugin.name !== spec.id ||
      plugin.$schema !== 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json'
    )
      throw new Error(`Invalid plugin manifest/version: ${spec.id}`);
    const skills = entries
      .filter((entry) => /^skills\/[^/]+\/SKILL.md$/.test(entry.name))
      .map((entry) => entry.name.split('/')[1]);
    if (!skills.length) throw new Error(`No skills in plugin: ${spec.id}`);
    for (const skill of skills) {
      const source = get(`skills/${skill}/SKILL.md`);
      if (
        !source.startsWith('---\n') ||
        !source.includes(`\nname: ${skill}\n`) ||
        !/\ndescription: .+/.test(source)
      )
        throw new Error(`Invalid skill metadata: ${skill}`);
    }
    for (const [target, source] of Object.entries(spec.shared ?? {})) {
      if (
        !allowed(target) || spec.files.includes(target) ||
        !/^(?:lib|bin)\//.test(target) ||
        !/^shared\/(?:[a-z0-9_]+\.py|runtime\.lock|bin\/(?:noxgate(?:\.sh|\.cmd|\.ps1)?|install\.py))$/.test(source)
      )
        throw new Error(`Invalid shared source: ${source}`);
      const data = await safeRead(sourceRoot, source);
      const executable = target === 'bin/noxgate' || target === 'bin/noxgate.sh';
      entries.push({ name: target, data, executable });
      if (syncShared) {
        // Keep the Git-backed Codex marketplace installable directly from a checkout.
        const sharedDir = dirname(join(pluginRoot, target));
        await mkdir(sharedDir, { recursive: true });
        if ((await lstat(sharedDir)).isSymbolicLink())
          throw new Error(`Symlink in plugin: ${sharedDir}`);
        const existing = await safeRead(pluginRoot, target).catch((error) => {
          if (error.code !== 'ENOENT') throw error;
          return null;
        });
        const path = join(pluginRoot, target);
        if (!existing?.equals(data)) await writeFile(path, data);
        if (executable && process.platform !== 'win32') await chmod(path, 0o755);
      }
    }
    if (platform) {
      entries.push({ name: 'runtime/platform.txt', data: Buffer.from(platform + '\n') });
      entries.push(...runtime.filter(({ group }) => group !== 'game' || skills.includes('noxgate-image-assets')));
    }
    const data = zip(entries.slice().sort((a, b) => a.name.localeCompare(b.name)));
    const filename = `${spec.id}-${version}${platform ? '-' + platform : ''}.zip`;
    packages.push({
      entries,
      data,
      metadata: {
        id: spec.id,
        version,
        filename,
        url: `/downloads/plugins/${filename}`,
        sha256: createHash('sha256').update(data).digest('hex'),
        size: data.length,
        changelog,
        skills
      }
    });
  }
  const market = JSON.parse(await safeRead(root, '.agents/plugins/marketplace.json'));
  if (market.name !== 'noxgate' || !Array.isArray(market.plugins) || market.plugins.length !== ids.size)
    throw new Error('Invalid Codex marketplace');
  market.plugins.sort((a, b) => a.name.localeCompare(b.name));
  for (const [index, { metadata }] of packages.entries()) {
    const entry = market.plugins[index];
    if (entry.name !== metadata.id || entry.source?.source !== 'local' ||
        entry.source.path !== `./plugins/${metadata.id}`)
      throw new Error(`Invalid Codex marketplace entry: ${metadata.id}`);
  }
  const bundleEntries = [
    {
      name: '.agents/plugins/marketplace.json',
      data: Buffer.from(`${JSON.stringify(market, null, 2)}\n`)
    },
    ...packages.flatMap((pkg) =>
      pkg.entries.map((entry) => ({ ...entry, name: `plugins/${pkg.metadata.id}/${entry.name}` }))
    )
  ];
  const bundle = zip(bundleEntries.sort((a, b) => a.name.localeCompare(b.name)));
  const bundleName = `noxgate-marketplace-${catalog.marketplaceVersion}${platform ? '-' + platform : ''}.zip`;
  const marketplace = {
    name: market.name,
    version: catalog.marketplaceVersion,
    filename: bundleName,
    url: `/downloads/plugins/${bundleName}`,
    sha256: createHash('sha256').update(bundle).digest('hex'),
    size: bundle.length
  };
  const manifest = { schemaVersion: 1, marketplace, plugins: packages.map((pkg) => pkg.metadata) };
  await mkdir(outputRoot, { recursive: true });
  for (const { data, metadata } of [...packages, { data: bundle, metadata: marketplace }]) {
    await writeFile(join(outputRoot, metadata.filename), data);
    await writeFile(
      join(outputRoot, `${metadata.filename}.sha256`),
      `${metadata.sha256}  ${metadata.filename}\n`
    );
  }
  await writeFile(join(outputRoot, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
  return manifest;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const host = { darwin: 'macos', win32: 'windows', linux: 'linux' }[process.platform] + '-' + process.arch;
  const [option, platform, ...extra] = process.argv.slice(2);
  if (extra.length || (platform && option !== '--platform') ||
      (option && !['--source-only', '--all', '--platform'].includes(option)) ||
      (option === '--platform' && !platform)) {
    throw new Error('Usage: node scripts/package-plugins.mjs [--all | --platform <platform> | --source-only]');
  }
  const targets = option === '--all' ? targetPlatforms : [option === '--source-only' ? null : platform ?? host];
  for (const target of targets) {
    const outputRoot = join(repoRoot, 'dist', target ?? 'source');
    await packagePlugins({ syncShared: true, outputRoot, platform: target });
    console.log(`Packaged NoxGate plugins in ${outputRoot}`);
  }
}
