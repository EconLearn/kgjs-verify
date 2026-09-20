# kgjs-verify

Tools for checking that a [kgjs / KineticGraphs](https://github.com/cmakler/kgjs) graph definition
is actually correct, including the errors that a JSON Schema cannot see.

This exists because of a conversation with Chris Makler about building an AI interface for kgjs:
describe a graph in English, get the YAML. The hard part he identified is that the relationships
between objects matter, and a model can easily produce YAML that looks right and draws the wrong
graph. Generation is only useful if the output can be checked automatically, so the checker came
first.

By Jude Wallis. Built with the help of an AI coding assistant; I ran and verified everything here.

## The problem

Take this definition:

```yaml
layout:
  OneGraph:
    graph:
      objects:
      - Point:
          coordinates: [calcs.nope, 4]
```

`calcs.nope` was never declared. The JSON Schema accepts it, because the value is a string and a
string is legal there. The engine builds a view without throwing. Nothing reports a problem, and
the graph is wrong. That is the failure mode that matters when the YAML was written by a model.

## What is here

`src/check.js` runs four stages and reports machine-readable JSON, so a failure can be fed
straight back to a generator as a repair prompt:

1. **parse** — is it YAML?
2. **schema** — does it match the [generated JSON Schema](https://github.com/EconLearn/kgjs-schema)? Catches `Pont` for `Point`, misspelled properties, wrong value types.
3. **render** — does the real engine build a `KG.View` under jsdom?
4. **content** — did anything get drawn, or did it silently render an empty graph?

`src/refcheck.js` does the part a schema cannot. It collects every declared name and every
`params.x` / `calcs.x` / `colors.x` / `custom.x` reference, and reports references with no
declaration, with the path to the offending value and a suggested correction.

Getting that right needed two pieces of engine behaviour, both read out of the kgjs source rather
than hardcoded, so they stay correct as kgjs changes:

- every schema installs a base colour palette (blue, orange, green, red, purple, brown, magenta,
  grey, gray, olive), so `colors.blue` is valid with no `colors:` block;
- naming an object registers a calc under that name, so `supply: {name: ourSupply}` makes
  `calcs.ourSupply` valid.

Without those, the checker reported 9 false positives on the kgjs docs. With them it reports one,
and that one looks real (below).

## Results on the kgjs documentation

108 examples extracted from the docs, 294 references resolved.

| | |
|---|---|
| examples checked | 108 |
| references resolved | 294 |
| dangling references reported | 1 |
| planted bad reference still caught | yes |

### The one report

`docs/econ/linear-supply-demand.md` has two arrows showing how a shift moves the curve. The
demand one reads:

```yaml
- Arrow:
    begin: [calcs.oldDemand.a.x, 4]
    end: [calcs.newDemand.a.x, 4]
```

and the demand curve above it is declared `demand: {name: oldDemand, ...}`. The supply one reads:

```yaml
- Arrow:
    begin: [calcs.supply.a.x, 8]
    end: [calcs.newSupply.a.x, 8]
```

but the first supply curve in that definition has no `name:` at all, so nothing called `supply`
exists. By symmetry with the demand side it should be `name: oldSupply` on the curve and
`calcs.oldSupply.a.x` in the arrow.

I could not confirm what this looks like on screen. jsdom has no layout, so element coordinates
come back empty whether the reference resolves or not, and I could not isolate the effect on the
live page either. So this is an undeclared reference and an inconsistency with the demand side,
not a confirmed visual bug.

## Corpus

`corpus/kgjs_examples.jsonl` holds the 108 examples with the prose that introduces each one. Every
example in the docs sits under text explaining what the graph shows, which makes the documentation
a labelled set of description-and-YAML pairs. That is the evaluation set for the generator.

## Running it

```bash
git clone https://github.com/cmakler/kgjs ../kgjs
npm install js-yaml ajv jsdom
node src/check.js    --repo ../kgjs --schema <kg.strict.schema.json> --bundle ../kgjs/docs/js/kg.0.3.3.js graph.yml
node src/refcheck.js --repo ../kgjs graph.yml
```

## Next

The generator. The loop is: describe, generate, run both checkers, feed any failure back, repeat.
The checkers are the part that makes that loop trustworthy, which is why they came first.
