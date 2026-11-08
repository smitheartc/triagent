import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';

import { api, ApiError } from '../api';
import type { ScoreRow } from '../contracts';
import { count, pct, seconds } from '../format';
import { mono, usePalette, type Tone } from '../theme';
import { Badge, Empty, Loading, Notice, Pairs, s, Section } from '../ui';
import { usePoll } from '../usePoll';

/** Scoreboard: GET /scores/latest, the newest server/scorer.py run (report vs. the dcsim answer key). */
export default function Scores() {
  const p = usePalette();
  const { data, error, refresh, refreshing } = usePoll(api.latestScores, () => false);

  if (!data) {
    if (error instanceof ApiError && error.status === 404) {
      return <Empty title="No scorer run yet" detail="Run python3 server/scorer.py --runs 3; the newest run shows here." />;
    }
    return error ? (
      <View style={s.page}>
        <Notice tone="red" title="Can't load scores">
          {error.message}
        </Notice>
      </View>
    ) : (
      <Loading />
    );
  }

  const { meta, rows } = data;
  const models = (meta.models ?? {}) as Record<string, string>;
  const passed = rows.filter((r) => r.passed).length;
  const before = rows.filter((r) => before_(r)?.passed).length;
  const herrings = rows.filter((r) => r.red_herring).length;
  const scenarios = [...new Set(rows.map((r) => r.scenario))];

  return (
    <ScrollView
      contentContainerStyle={s.page}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} tintColor={p.muted} />}
    >
      <Section title="Latest run" aside={<Text style={[s.tiny, { color: p.faint, fontFamily: mono }]}>{stamp(meta.stamp)}</Text>}>
        <View style={score.stats}>
          <Stat value={`${passed}/${rows.length}`} label="runs passed" tone={passed === rows.length ? 'green' : 'amber'} />
          <Stat value={`${before}/${rows.length}`} label="before review" tone="muted" />
          <Stat value={String(herrings)} label="red herrings cited" tone={herrings ? 'red' : 'green'} />
        </View>
        <Pairs
          data={{
            investigator: models.investigator,
            critic: models.critic,
            'runs each': meta.runs,
            data: meta.fake ? 'fake_server sample, not a real run' : undefined,
          }}
        />
      </Section>
      {scenarios.map((name) => (
        <Scenario key={name} name={name} runs={rows.filter((r) => r.scenario === name)} />
      ))}
    </ScrollView>
  );
}

function Scenario({ name, runs }: { name: string; runs: ScoreRow[] }) {
  const p = usePalette();
  const passed = runs.filter((r) => r.passed).length;
  const done = runs.filter((r) => r.status === 'done');
  const entity = runs.find((r) => r.report_entity)?.report_entity;
  const category = runs.find((r) => r.report_category)?.report_category;
  const failedChecks = [
    ...new Set(runs.flatMap((r) => (Array.isArray(r.critic_failed) ? r.critic_failed.map(String) : []))),
  ];
  const retried = runs.filter((r) => r.retried).length;
  return (
    <Section
      title={name.replace(/_/g, ' ')}
      aside={<Badge label={`${passed}/${runs.length} passed`} tone={passed === runs.length ? 'green' : passed ? 'amber' : 'red'} />}
    >
      <View style={s.row}>
        <Check ok={all(runs, 'entity_ok')} label={`cause ${String(entity ?? '–')}`} />
        <Check ok={all(runs, 'category_ok')} label={`category ${String(category ?? '–')}`} />
        {runs.some((r) => r.red_herring) ? <Badge label="red herring cited" tone="red" /> : null}
      </View>
      <Text style={[s.small, { color: p.muted }]}>
        chain overlap {fixed(mean(done, 'jaccard'))} · confidence {pct(mean(done, 'confidence'))} ·{' '}
        {fixed(mean(runs, 'tool_calls'), 0)} tool calls · {seconds(mean(runs, 'seconds'))} ·{' '}
        {count(mean(runs, 'tokens'))} tokens
      </Text>
      <Text style={[s.small, { color: p.muted }]}>
        review:{' '}
        {retried
          ? `failed first in ${retried} of ${runs.length}` +
            (failedChecks.length ? ` (${failedChecks.join(', ').replace(/_/g, ' ')})` : '') +
            ', then retried'
          : runs.every((r) => r.critic_first === 'pass')
            ? 'passed first time'
            : runs.map((r) => r.critic_first ?? '–').join(', ')}
      </Text>
      {runs.length > 1 ? (
        <View style={s.row}>
          {runs.map((r) => (
            <View
              key={r.run}
              style={[score.dot, { backgroundColor: r.red_herring ? p.red : r.passed ? p.green : p.amber }]}
              accessibilityLabel={`run ${r.run} ${r.passed ? 'passed' : 'failed'}`}
            />
          ))}
        </View>
      ) : null}
      {runs
        .filter((r) => r.status !== 'done')
        .map((r) => (
          <Text key={r.run} style={[s.small, { color: p.red }]}>
            run {r.run}: {r.status}
            {r.error ? ` – ${String(r.error)}` : ''}
          </Text>
        ))}
    </Section>
  );
}

function Stat({ value, label, tone }: { value: string; label: string; tone: Tone }) {
  const p = usePalette();
  return (
    <View style={score.stat}>
      <Text style={[score.statValue, { color: tone === 'muted' ? p.ink : p[tone] }]}>{value}</Text>
      <Text style={[s.tiny, { color: p.muted }]}>{label}</Text>
    </View>
  );
}

function Check({ ok, label }: { ok: boolean | null; label: string }) {
  return <Badge label={`${ok == null ? '–' : ok ? '✓' : '✗'} ${label}`} tone={ok == null ? 'muted' : ok ? 'green' : 'red'} />;
}

const before_ = (r: ScoreRow) => r.before as { passed?: boolean } | undefined;

function all(runs: ScoreRow[], key: 'entity_ok' | 'category_ok'): boolean | null {
  const xs = runs.map((r) => r[key]).filter((v): v is boolean => typeof v === 'boolean');
  return xs.length ? xs.every(Boolean) : null;
}

function mean(runs: ScoreRow[], key: keyof ScoreRow): number | null {
  const xs = runs.map((r) => r[key]).filter((v): v is number => typeof v === 'number');
  return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
}

const fixed = (x: number | null, digits = 2) => (x == null ? '–' : x.toFixed(digits));

/** 20261108T000000Z -> 2026-11-08 00:00Z */
function stamp(v: unknown): string {
  const m = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})/.exec(String(v ?? ''));
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}Z` : String(v ?? '');
}

const score = StyleSheet.create({
  stats: { flexDirection: 'row', gap: 8 },
  stat: { flex: 1, gap: 2 },
  statValue: { fontSize: 26, lineHeight: 32, fontWeight: '700', fontVariant: ['tabular-nums'] },
  dot: { width: 10, height: 10, borderRadius: 5 },
});
