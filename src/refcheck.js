#!/usr/bin/env node
/*
 * refcheck.js -- static check for dangling references in a KGJS definition.
 *
 * kgjs expressions refer to declared things by name: params.x, calcs.slope, colors.demand,
 * custom.title. Nothing enforces that the name exists. A definition with a typo in one of those
 * names passes the JSON Schema (the value is a string, which is legal) and builds a KG.View
 * without throwing, then quietly draws the wrong graph. That is the failure mode a schema cannot
 * reach, and it is the one that matters when the YAML was written by a model rather than a person.
 *
 * This collects every declared name, then every reference, and reports references with no
 * declaration. Usage: refcheck.js file.yml [...]
 */
'use strict';
const fs = require('fs');
const yaml = require('js-yaml');

const NS = ['params', 'calcs', 'colors', 'custom'];

/* Two things are declared implicitly by the engine rather than by the author, and a checker that
 * does not know about them reports false positives on correct graphs:
 *   - a base colour palette every schema installs (src/ts/KGAuthor/schemas/schema.ts), so
 *     colors.blue works with no colors: block;
 *   - naming an object registers a calc under that name, so `- EconLinearSupply: {name: ourSupply}`
 *     makes calcs.ourSupply valid.
 * The palette is read out of the kgjs source when a checkout is given, so it stays right as kgjs
 * changes, and falls back to the known set otherwise. */
const FALLBACK_PALETTE = ['blue','orange','green','red','purple','brown','magenta','grey','gray','olive'];

function paletteFrom(repo) {
    if (!repo) return FALLBACK_PALETTE;
    try {
        const src = fs.readFileSync(require('path').join(repo, 'src/ts/KGAuthor/schemas/schema.ts'), 'utf8');
        const block = src.slice(src.indexOf('const palette'), src.indexOf('};', src.indexOf('const palette')));
        const names = [...block.matchAll(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:/gm)].map(m => m[1]);
        return names.length ? names : FALLBACK_PALETTE;
    } catch (e) { return FALLBACK_PALETTE; }
}

/* Schemas install their own named colours on top of the base palette (EconSchema adds
 * demand, supply, budget, utility and so on). Read them out of the schema sources so the set
 * tracks kgjs rather than drifting. */
const SCHEMA_FILES = {
    EconSchema: 'src/ts/KGAuthor/econ/schemas/econSchema.ts',
    LowdownSchema: 'src/ts/KGAuthor/econ/schemas/lowdownSchema.ts',
    BowlesHallidaySchema: 'src/ts/KGAuthor/econ/schemas/bowlesHallidaySchema.ts',
};

function schemaColors(repo, schemaName) {
    const rel = SCHEMA_FILES[schemaName];
    if (!repo || !rel) return [];
    try {
        const src = fs.readFileSync(require('path').join(repo, rel), 'utf8');
        const i = src.indexOf('def.colors = KG.setDefaults');
        if (i < 0) return [];
        const block = src.slice(i, src.indexOf('});', i));
        return [...block.matchAll(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:/gm)].map(m => m[1]);
    } catch (e) { return []; }
}

function namedObjects(node, out) {
    if (Array.isArray(node)) node.forEach(v => namedObjects(v, out));
    else if (node && typeof node === 'object') {
        if (typeof node.name === 'string') out.add(node.name);
        Object.values(node).forEach(v => namedObjects(v, out));
    }
    return out;
}
const REF = /\b(params|calcs|colors|custom)\.([A-Za-z_][A-Za-z0-9_]*)/g;

function declared(doc, palette) {
    const out = {params: new Set(), calcs: new Set(), colors: new Set(palette), custom: new Set()};
    if (!doc || typeof doc !== 'object') return out;
    // params is a list of {name: ...}; the others are maps
    if (Array.isArray(doc.params)) for (const p of doc.params) if (p && p.name) out.params.add(String(p.name));
    else if (doc.params && typeof doc.params === 'object') Object.keys(doc.params).forEach(k => out.params.add(k));
    for (const ns of ['calcs', 'colors', 'custom']) {
        const v = doc[ns];
        if (v && typeof v === 'object' && !Array.isArray(v)) Object.keys(v).forEach(k => out[ns].add(k));
    }
    namedObjects(doc, out.calcs);       // naming an object registers a calc
    return out;
}

function references(node, path, found) {
    if (typeof node === 'string') {
        let m;
        REF.lastIndex = 0;
        while ((m = REF.exec(node)) !== null) found.push({ns: m[1], name: m[2], at: path, text: node.slice(0, 80)});
    } else if (Array.isArray(node)) {
        node.forEach((v, i) => references(v, path + '/' + i, found));
    } else if (node && typeof node === 'object') {
        for (const k of Object.keys(node)) {
            let m; REF.lastIndex = 0;
            while ((m = REF.exec(k)) !== null) found.push({ns: m[1], name: m[2], at: path, text: k});
            references(node[k], path + '/' + k, found);
        }
    }
    return found;
}

function check(file, palette, repo) {
    const doc = yaml.load(fs.readFileSync(file, 'utf8'));
    const extra = (doc && typeof doc.schema === 'string') ? schemaColors(repo, doc.schema) : [];
    const decl = declared(doc, palette.concat(extra));
    const refs = references(doc, '', []);
    const dangling = [];
    const seen = new Set();
    for (const r of refs) {
        const key = r.ns + '.' + r.name + '@' + r.at;
        if (seen.has(key)) continue;
        seen.add(key);
        if (!decl[r.ns].has(r.name)) {
            // suggest the closest declared name
            const cands = [...decl[r.ns]];
            let best = null, bestD = 1e9;
            for (const c of cands) {
                const d = lev(c, r.name);
                if (d < bestD) { bestD = d; best = c; }
            }
            dangling.push({
                reference: r.ns + '.' + r.name, at: r.at || '/', in: r.text,
                declared_in_that_namespace: cands.length,
                did_you_mean: (best && bestD <= Math.max(2, Math.floor(r.name.length / 3))) ? r.ns + '.' + best : null,
            });
        }
    }
    return {
        file,
        declared: Object.fromEntries(NS.map(n => [n, [...decl[n]]])),
        references_found: refs.length,
        dangling,
        ok: dangling.length === 0,
    };
}

function lev(a, b) {
    const m = a.length, n = b.length;
    const d = Array.from({length: m + 1}, (_, i) => [i, ...Array(n).fill(0)]);
    for (let j = 1; j <= n; j++) d[0][j] = j;
    for (let i = 1; i <= m; i++) for (let j = 1; j <= n; j++)
        d[i][j] = Math.min(d[i-1][j] + 1, d[i][j-1] + 1, d[i-1][j-1] + (a[i-1] === b[j-1] ? 0 : 1));
    return d[m][n];
}

const argv = process.argv.slice(2);
let repo = null; const files = [];
for (let i = 0; i < argv.length; i++) { if (argv[i] === '--repo') repo = argv[++i]; else files.push(argv[i]); }
const palette = paletteFrom(repo);
const res = files.map(f => check(f, palette, repo));
console.log(JSON.stringify(res.length === 1 ? res[0] : res, null, 2));
process.exit(res.some(r => !r.ok) ? 1 : 0);
