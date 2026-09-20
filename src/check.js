#!/usr/bin/env node
/*
 * check.js -- verify one KGJS graph definition end to end and report machine-readable errors.
 *
 * Four stages, each of which can fail on its own:
 *   1. parse    -- is it YAML at all?
 *   2. schema   -- does it match the generated JSON Schema (strict: unknown keys rejected)?
 *   3. render   -- does the real kgjs engine build a KG.View from it under jsdom?
 *   4. content  -- did anything actually get drawn, and which object types?
 *
 * Stage 2 catches misspelled object types and properties. Stage 3 catches the semantic errors a
 * schema cannot see: an expression referring to a calc that does not exist, a coordinate pointing
 * at a parameter that was never declared. Stage 4 catches the quiet failure where a definition is
 * legal and renders an empty graph.
 *
 * The output is JSON so it can be fed straight back to a generator as a repair prompt.
 *
 * Usage: check.js --repo <kgjs> --schema <kg.strict.schema.json> [--bundle <kg.x.y.z.js>] file.yml
 */
'use strict';
const fs = require('fs');
const path = require('path');
const yaml = require('js-yaml');
const Ajv = require('ajv');
let JSDOM, VirtualConsole;
try { ({JSDOM, VirtualConsole} = require('jsdom')); } catch (e) { JSDOM = null; }

function renderer(scripts) {
    const sources = scripts.map(f => fs.readFileSync(f, 'utf8'));
    return function (data) {
        const vc = new VirtualConsole();
        const dom = new JSDOM('<!doctype html><html><body><div id="g" class="kg-container" style="width:600px"></div></body></html>',
            {runScripts: 'outside-only', virtualConsole: vc, pretendToBeVisual: true});
        const w = dom.window;
        try {
            sources.forEach((src, i) => w.eval(src + (i === sources.length - 1 ? '\n;window.KG = KG;' : '')));
        } catch (e) { return {ok: false, stage: 'engine', error: 'engine did not load: ' + e.message}; }
        w.__data = JSON.parse(JSON.stringify(data).replace(/&gt;/g, '>').replace(/&lt;/g, '<').replace(/&amp;/g, '&'));
        try {
            w.eval('window.__view = new KG.View(document.getElementById("g"), window.__data)');
        } catch (e) {
            const where = ((e && e.stack) || '').split('\n').find(l => /at new |at [A-Za-z]+\.[a-zA-Z]+ /.test(l)) || '';
            return {ok: false, stage: 'render',
                    error: ((e && e.constructor && e.constructor.name) || 'Error') + ': ' + (e && e.message),
                    where: where.trim().replace(/\(eval at.*$/, '').trim()};
        }
        // what actually ended up on the page
        let drawn = {svg: 0, marks: 0, types: []};
        try {
            drawn.svg = w.document.querySelectorAll('svg').length;
            drawn.marks = w.document.querySelectorAll('svg path, svg circle, svg line, svg rect, svg text').length;
            drawn.types = w.eval('(window.__view && window.__view.model && window.__view.model.objects ? window.__view.model.objects.map(function(o){return o.constructor && o.constructor.name}) : [])') || [];
        } catch (e) { /* introspection is best-effort */ }
        w.close();
        return {ok: true, drawn: drawn};
    };
}

function main() {
    const a = process.argv.slice(2);
    let repo = null, schemaPath = null, bundle = null; const files = [];
    for (let i = 0; i < a.length; i++) {
        if (a[i] === '--repo') repo = a[++i];
        else if (a[i] === '--schema') schemaPath = a[++i];
        else if (a[i] === '--bundle') bundle = a[++i];
        else files.push(a[i]);
    }
    const schema = JSON.parse(fs.readFileSync(schemaPath, 'utf8'));
    const ajv = new Ajv({allErrors: true, strict: false});
    const validate = ajv.compile(schema);
    const scripts = bundle ? [path.resolve(bundle)]
        : [path.join(repo, 'build', 'lib', 'kg-lib.js'), path.join(repo, 'build', 'kg.js')];
    const render = JSDOM ? renderer(scripts) : null;

    const out = [];
    for (const f of files) {
        const text = fs.readFileSync(f, 'utf8');
        const rec = {file: f, parse: null, schema: null, render: null};
        let data;
        try { data = yaml.load(text); rec.parse = {ok: true}; }
        catch (e) { rec.parse = {ok: false, error: String(e.message).split('\n')[0]}; out.push(rec); continue; }

        const valid = validate(data);
        rec.schema = valid ? {ok: true} : {ok: false, errors: (validate.errors || []).slice(0, 8).map(e => ({
            at: e.instancePath || '/', problem: e.message,
            offending: e.params && (e.params.additionalProperty || e.params.allowedValues || undefined)
        }))};

        if (render) rec.render = render(data);
        else rec.render = {ok: null, note: 'jsdom not installed'};
        if (rec.render && rec.render.ok && rec.render.drawn && rec.render.drawn.marks === 0)
            rec.render.warning = 'rendered without error but drew nothing';
        out.push(rec);
    }
    console.log(JSON.stringify(out.length === 1 ? out[0] : out, null, 2));
    const bad = out.some(r => !r.parse.ok || !r.schema.ok || (r.render && r.render.ok === false));
    process.exit(bad ? 1 : 0);
}
main();
