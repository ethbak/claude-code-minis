// Builds the site's pages from the repo READMEs, so the READMEs stay the one source of truth. Each page gets a title
// and description from its README, and JSON-LD (SoftwareSourceCode, plus FAQPage from its "FAQ" section) for
// search engines and answer engines. GitHub-only markup is translated: the header image is dropped (the site shows
// the title), GitHub alerts become Starlight asides, and repo-relative links and images are rewritten.
import { cpSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, posix } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const out = join(root, 'site', 'src', 'content', 'docs');
const repo = 'https://github.com/ethbak/claude-code-minis';
const base = '/claude-code-minis';
const pages = [
  {
    readme: 'README.md', slug: 'index', name: 'claude-code-minis',
    title: 'claude-code-minis: small, focused plugins for Claude Code',
    description: 'Small, focused plugins for Claude Code: resume any session, run shell commands and switch '
      + 'permission modes from the Claude app.',
  },
  { readme: 'plugins/remote-resume/README.md', slug: 'remote-resume', name: 'remote-resume' },
  { readme: 'plugins/remote-terminal/README.md', slug: 'remote-terminal', name: 'remote-terminal' },
  { readme: 'plugins/mode-picker/README.md', slug: 'mode-picker', name: 'mode-picker' },
  { readme: 'docs/waiting-for-input.md', slug: 'waiting-for-input', name: 'remote-terminal' },
];
const ASIDES = { NOTE: 'note', TIP: 'tip', IMPORTANT: 'note', WARNING: 'caution', CAUTION: 'danger' };

const plain = (md) => md.replace(/<[^>]+>/g, '').replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/[`*_]/g, '').trim();

function faq(body) {
  const section = body.split(/^## \S* ?FAQ\s*$/m)[1];
  if (!section) return [];
  return [...section.split(/^## /m)[0].matchAll(/<summary>([\s\S]+?)<\/summary>([\s\S]+?)<\/details>/g)]
    .map(([, q, a]) => ({ '@type': 'Question', name: plain(q), acceptedAnswer: { '@type': 'Answer', text: plain(a) } }));
}

// Where a repo-relative link from <readme> points on the site: another page, or the file on GitHub.
function target(readme, link) {
  const path = posix.normalize(posix.join(dirname(readme), link)).replace(/\/$/, '');
  if (path === '.') return `${base}/`;
  const plugin = path.match(/^plugins\/([\w-]+)$/);
  if (plugin) return `${base}/${plugin[1]}/`;
  const doc = path.match(/^docs\/([\w-]+)\.md$/);
  if (doc) return `${base}/${doc[1]}/`;
  if (path.startsWith('assets/')) return `${base}/${path}`;
  return `${repo}/blob/main/${path}`;
}

function translate(readme, md) {
  const local = /^(?!https?:|#|\/|mailto:)/;
  return md
    // GitHub alerts: "> [!NOTE]" and the quoted lines after it.
    .replace(/^> \[!(\w+)\]\n((?:>.*\n?)*)/gm, (_, kind, lines) =>
      `:::${ASIDES[kind.toUpperCase()] || 'note'}\n${lines.replace(/^> ?/gm, '').trimEnd()}\n:::\n`)
    .replace(/\]\(([^)]+)\)/g, (all, link) => local.test(link) ? `](${target(readme, link)})` : all)
    .replace(/(href|src|srcset)="([^"]+)"/g, (all, attr, link) => local.test(link) ? `${attr}="${target(readme, link)}"` : all);
}

mkdirSync(out, { recursive: true });
cpSync(join(root, 'assets'), join(root, 'site', 'public', 'assets'), { recursive: true });
for (const page of pages) {
  let text = readFileSync(join(root, page.readme), 'utf8').replace(/^(<p[^>]*>\s*)?<picture>[\s\S]*?<\/picture>(\s*<\/p>)?\s*/, '');
  let { title, description } = page;
  if (!title) {
    const heading = text.match(/^# (.+)\n/m);
    title = heading[1];
    text = text.slice(heading.index + heading[0].length);
    description = plain(text.trim().split('\n\n')[0]).replace(/\s+/g, ' ');
  }
  const graph = [{
    '@type': 'SoftwareSourceCode', name: page.name, description, codeRepository: repo,
    programmingLanguage: 'Python', license: 'https://opensource.org/licenses/MIT',
    url: `https://ethbak.github.io${base}/${page.slug === 'index' ? '' : page.slug + '/'}`,
  }];
  const questions = faq(text);
  if (questions.length) graph.push({ '@type': 'FAQPage', mainEntity: questions });
  const jsonld = JSON.stringify({ '@context': 'https://schema.org', '@graph': graph });
  const front = [
    '---',
    `title: ${JSON.stringify(title)}`,
    `description: ${JSON.stringify(description.slice(0, 300))}`,
    'head:',
    '  - tag: script',
    '    attrs:',
    '      type: application/ld+json',
    `    content: ${JSON.stringify(jsonld)}`,
    `editUrl: ${repo}/edit/main/${page.readme}`,
    '---',
    '',
  ].join('\n');
  writeFileSync(join(out, `${page.slug}.md`), front + translate(page.readme, text).trimStart());
  console.log(`${page.slug}: ${questions.length} FAQ entries`);
}
