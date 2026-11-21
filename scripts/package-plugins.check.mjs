import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
import { readFile, mkdir, mkdtemp, writeFile, rm, access, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { inflateRawSync } from 'node:zlib';
import { test } from 'node:test';
import { packagePlugins } from './package-plugins.mjs';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function zipEntries(archive) {
  let cursor = archive.length - 22;
  while (cursor >= 0 && archive.readUInt32LE(cursor) !== 0x06054b50) cursor--;
  assert.ok(cursor >= 0, 'ZIP end record exists');
  let central = archive.readUInt32LE(cursor + 16);
  const files = new Map();
  for (let i = 0; i < archive.readUInt16LE(cursor + 10); i++) {
    assert.equal(archive.readUInt32LE(central), 0x02014b50);
    const nameLength = archive.readUInt16LE(central + 28);
    const name = archive.subarray(central + 46, central + 46 + nameLength).toString();
    const local = archive.readUInt32LE(central + 42);
    const dataStart =
      local + 30 + archive.readUInt16LE(local + 26) + archive.readUInt16LE(local + 28);
    const compressedSize = archive.readUInt32LE(central + 20);
    files.set(name, inflateRawSync(archive.subarray(dataStart, dataStart + compressedSize)));
    central +=
      46 + nameLength + archive.readUInt16LE(central + 30) + archive.readUInt16LE(central + 32);
  }
  return files;
}

test('packages the two NoxGate plugins with matching manifests and no credentials', async (context) => {
  const outputRoot = await mkdtemp(join(tmpdir(), 'noxgate-package-'));
  context.after(() => rm(outputRoot, { recursive: true, force: true }));
  const manifest = await packagePlugins({ outputRoot });
  assert.deepEqual(JSON.parse(await readFile(join(outputRoot, 'manifest.json'), 'utf8')), manifest);
  const sharedRuntime = await readFile(join(repoRoot, 'plugins/shared/noxgate_runtime.py'));
  assert.deepEqual(
    manifest.plugins.map((plugin) => plugin.id),
    ['noxgate-game-assets', 'noxgate-media']
  );
  assert.equal(manifest.marketplace.name, 'noxgate');
  const files = await Promise.all(
    manifest.plugins.map(async (plugin) => {
      const archive = await readFile(
        join(outputRoot, plugin.filename)
      );
      assert.equal(createHash('sha256').update(archive).digest('hex'), plugin.sha256);
      assert.equal(archive.length, plugin.size);
      const files = zipEntries(archive);
      const names = [...files.keys()];
      assert.ok(names.includes('plugin.json'));
      assert.deepEqual(files.get('lib/noxgate_runtime.py'), sharedRuntime);
      for (const name of ['noxgate_auth.py', 'noxgate_credentials.py', 'noxgate_recovery.py']) {
        assert.deepEqual(
          files.get('lib/' + name),
          await readFile(join(repoRoot, 'plugins/shared', name))
        );
      }
      for (const name of ['noxgate', 'install.py']) {
        assert.deepEqual(
          files.get('bin/' + name),
          await readFile(join(repoRoot, 'plugins/shared/bin', name))
        );
      }
      const extraction = await mkdtemp(join(tmpdir(), 'noxgate-plugin-zip-'));
      try {
        for (const [name, data] of files) {
          const target = join(extraction, name);
          await mkdir(dirname(target), { recursive: true });
          await writeFile(target, data);
        }
        const env = {
          ...process.env,
          NOXGATE_HOME: join(extraction, 'empty-profile'),
          PYTHONDONTWRITEBYTECODE: '1'
        };
        const help = execFileSync('python3', [join(extraction, 'bin/noxgate'), '--help'], {
          env,
          encoding: 'utf8'
        });
        assert.ok(help.includes('configure-env'));
        assert.ok(help.includes('migrate-storage'));
        execFileSync(
          'python3',
          [
            join(extraction, 'bin/install.py'),
            '--skip-dependencies',
            '--home',
            join(extraction, 'installed'),
            '--bin-dir',
            join(extraction, 'commands')
          ],
          { env }
        );
        await assert.rejects(access(join(extraction, 'installed/venv')));
        const authHelp = execFileSync(
          'python3',
          [join(extraction, 'installed/cli/bin/noxgate'), 'auth', 'migrate-storage', '--help'],
          { env, encoding: 'utf8' }
        );
        assert.ok(authHelp.includes('--from'));
        assert.ok(authHelp.includes('--to'));
        assert.ok(authHelp.includes('--env-file'));
        const listing = execFileSync(
          'python3',
          [join(extraction, 'installed/cli/bin/noxgate'), 'auth', 'list'],
          { env, encoding: 'utf8' }
        );
        assert.deepEqual(JSON.parse(listing).profiles, []);
        const migrationHelp = execFileSync(
          'python3',
          [join(extraction, 'installed/cli/bin/noxgate'), 'tasks', 'migrate-state', '--help'],
          { env, encoding: 'utf8' }
        );
        assert.ok(migrationHelp.includes('--source-gateway'));
        assert.ok(migrationHelp.includes('--asset-root'));
        const output = join(extraction, 'completed.png');
        const state = join(extraction, 'legacy.task.json');
        const gateway = 'https://example.invalid/v1';
        await mkdir(env.NOXGATE_HOME, { recursive: true });
        await writeFile(output, Buffer.from('synthetic completed artifact'));
        const profilesPath = join(env.NOXGATE_HOME, 'profiles.json');
        await writeFile(profilesPath, JSON.stringify({ active: 'work', profiles: { work: { gateway } } }));
        await chmod(profilesPath, 0o600);
        await writeFile(
          state,
          JSON.stringify({
            schemaVersion: 1,
            kind: 'image',
            fingerprint: 'fixture',
            provider_hash: createHash('sha256').update(gateway).digest('hex'),
            status: 'completed',
            files: [output]
          })
        );
        const migrated = execFileSync(
          'python3',
          [
            join(extraction, 'installed/cli/bin/noxgate'),
            'tasks',
            'migrate-state',
            '--state',
            state,
            '--profile',
            'work'
          ],
          { env, encoding: 'utf8' }
        );
        assert.ok(migrated.includes('未重新提交'));
        assert.equal(JSON.parse(await readFile(state, 'utf8')).profile, 'work');
      } finally {
        await rm(extraction, { recursive: true, force: true });
      }
      assert.ok(
        names.every(
          (name) =>
            !name.includes('AGENTS.md') && !name.includes('__pycache__') && !name.includes('/test_')
        )
      );
      assert.ok(
        names.every(
          (name) =>
            !name.endsWith('/config.toml') &&
            !name.endsWith('/auth.json') &&
            !name.split('/').some((part) => part === '.env' || part.startsWith('.env.')) &&
            !name.includes('credentials.json') &&
            !name.includes('/cache/')
        )
      );
      const source = Buffer.concat([...files.values()]).toString('utf8');
      assert.equal(source.includes('api.evolink.ai'), false);
      return archive.length;
    })
  );
  assert.ok(files.every((size) => size > 100));
  const catalog = JSON.parse(await readFile(join(repoRoot, 'plugins/catalog.json'), 'utf8'));
  for (const spec of catalog.plugins) {
    for (const [target, source] of Object.entries(spec.shared)) {
      assert.deepEqual(
        await readFile(join(repoRoot, 'plugins', spec.id, target)),
        await readFile(join(repoRoot, 'plugins', source)),
        spec.id + '/' + target
      );
    }
  }

  const marketplace = await readFile(join(outputRoot, manifest.marketplace.filename));
  assert.equal(createHash('sha256').update(marketplace).digest('hex'), manifest.marketplace.sha256);
  const entries = zipEntries(marketplace);
  const market = JSON.parse(entries.get('.agents/plugins/marketplace.json').toString());
  assert.deepEqual(market.plugins.map(({ name }) => name), manifest.plugins.map(({ id }) => id));
  for (const plugin of manifest.plugins) {
    const archive = await readFile(join(outputRoot, plugin.filename));
    for (const [name, data] of zipEntries(archive)) {
      assert.deepEqual(entries.get(`plugins/${plugin.id}/${name}`), data);
    }
  }
});
