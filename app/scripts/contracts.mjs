// Regenerates src/contracts.schema.json and src/contracts.ts from server/contracts.py (the source of truth).
//   npm run contracts                      needs the server's Python deps (pip install -r ../server/requirements.txt)
//   PYTHON=../server/.venv/bin/python npm run contracts      to pick a specific interpreter
import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { compile } from 'json-schema-to-typescript';

const SCHEMA = 'src/contracts.schema.json';
const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
execFileSync(python, ['../server/contracts.py', '--schema', SCHEMA], { stdio: 'inherit' });

const schema = JSON.parse(readFileSync(SCHEMA, 'utf8'));
// pydantic titles every property, and json2ts turns each title into an exported alias (Status1, Text, Error, ...).
// Without property titles each model becomes one interface with its field types inline.
for (const model of Object.values(schema.$defs)) {
  for (const prop of Object.values(model.properties ?? {})) delete prop.title;
}
const ts = await compile(schema, 'TriagentContracts', {
  unreachableDefinitions: true,
  additionalProperties: false,
  unknownAny: true,
  bannerComment: '/* Generated from server/contracts.py by scripts/contracts.mjs. Do not edit; run `npm run contracts`. */',
});
writeFileSync('src/contracts.ts', ts);
console.log('wrote src/contracts.ts');
