import { router, Stack, useLocalSearchParams } from 'expo-router';
import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native';

import { api, TERMINAL } from '../../../api';
import type { Action, CriticResult, Job, Remediation, Report } from '../../../contracts';
import { ago, count, pct, seconds, utcDateTime, utcTime } from '../../../format';
import { mono, STATUS, usePalette } from '../../../theme';
import {
  Badge,
  Button,
  Code,
  EventIds,
  Loading,
  Meter,
  Notice,
  Pairs,
  PriorityBadge,
  s,
  Section,
  StatusBadge,
} from '../../../ui';
import { usePoll } from '../../../usePoll';

/** Incident detail: GET /jobs/{id}, re-polled every 2 s until the job is done or failed. */
export default function Incident() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const p = usePalette();
  const { data: job, error, refresh, refreshing } = usePoll(
    () => api.job(id),
    (j) => (j && TERMINAL.has(j.status) ? false : 2000),
  );

  if (!job) {
    return error ? (
      <View style={s.page}>
        <Notice tone="red" title="Can't load this incident">
          {error.message}
        </Notice>
      </View>
    ) : (
      <Loading />
    );
  }

  const live = !TERMINAL.has(job.status);
  const { report, remediation } = job;
  return (
    <ScrollView
      contentContainerStyle={s.page}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} tintColor={p.muted} />}
    >
      <Stack.Screen options={{ title: job.ticket_id }} />
      <Header job={job} />

      {live ? (
        <Notice tone="blue" title={`${STATUS[job.status]?.label ?? job.status}…`}>
          The agent is still working; this page fills in as it finishes.
        </Notice>
      ) : null}
      {job.status === 'failed' ? (
        <Notice tone="red" title="Investigation failed">
          {job.error || 'The server gave no error message.'}
        </Notice>
      ) : null}

      <View style={detail.buttons}>
        <View style={{ flex: 1 }}>
          <Button
            title={live ? 'Watch live' : 'Investigation trace'}
            kind={live ? 'primary' : 'plain'}
            onPress={() => router.push(`/jobs/${job.job_id}/live`)}
          />
        </View>
        <View style={{ flex: 1 }}>
          <Button
            title="Actions"
            kind={live ? 'plain' : 'primary'}
            onPress={() => router.push(`/jobs/${job.job_id}/actions`)}
          />
        </View>
      </View>

      {report ? <Findings report={report} critic={job.critic} /> : null}
      {remediation ? <RemediationSection remediation={remediation} /> : null}
      {report ? <Context report={report} /> : null}
      <Cost job={job} />
      {job.actions?.length ? <ActionHistory actions={job.actions} /> : null}
    </ScrollView>
  );
}

function Header({ job }: { job: Job }) {
  const p = usePalette();
  const t = job.ticket;
  const where = [t.service, t.host, ...Object.values(t.labels ?? {})].filter(Boolean).join(' · ');
  return (
    <View style={{ gap: 8 }}>
      <View style={s.row}>
        <PriorityBadge priority={t.priority ?? ''} />
        <StatusBadge status={job.status} />
      </View>
      <Text style={[s.title, { color: p.ink }]} selectable>
        {t.title}
      </Text>
      {t.description ? <Text style={[s.small, { color: p.muted }]}>{t.description}</Text> : null}
      <Text style={[s.small, { color: p.faint }]}>
        {where ? `${where} · ` : ''}opened {utcDateTime(t.created)} · received {ago(job.created)}
      </Text>
    </View>
  );
}

function Findings({ report, critic }: { report: Report; critic?: CriticResult | null }) {
  const p = usePalette();
  const rc = report.root_cause;
  const chain = report.causal_chain ?? [];
  const causeIds = new Set(rc.event_ids ?? []);
  const tone = report.confidence >= 0.7 ? 'green' : report.confidence >= 0.4 ? 'amber' : 'red';
  return (
    <>
      <Section title="Problem">
        <Text style={[s.body, { color: p.ink }]} selectable>
          {report.problem}
        </Text>
      </Section>

      <Section title="Root cause" aside={<Badge label={rc.category} tone={rc.category === 'unknown' ? 'amber' : 'blue'} />}>
        <View style={s.row}>
          <Text style={[s.body, { color: p.ink, fontFamily: mono, fontWeight: '700' }]} selectable>
            {rc.entity}
          </Text>
          <Text style={[s.small, { color: p.muted }]}>{rc.entity_type.replace(/_/g, ' ')}</Text>
        </View>
        <Text style={[s.body, { color: p.ink }]} selectable>
          {rc.summary}
        </Text>
        <Pairs data={rc.identifiers ?? {}} />
        <EventIds ids={rc.event_ids} />
        <View style={[s.row, { flexWrap: 'nowrap' }]}>
          <Text style={[s.small, { color: p.muted }]}>Confidence</Text>
          <Meter value={report.confidence} tone={tone} />
          <Text style={[s.small, { color: p.ink, fontWeight: '700' }]}>{pct(report.confidence)}</Text>
        </View>
        {critic ? <Review critic={critic} /> : null}
      </Section>

      {chain.length ? (
        <Section
          title="Evidence timeline"
          aside={<Text style={[s.tiny, { color: p.faint }]}>{utcDateTime(chain[0].timestamp).split(',')[0]} · UTC</Text>}
        >
          {chain.map((step, i) => {
            const cause = causeIds.has(step.event_id);
            return (
              <View key={`${step.event_id}-${i}`} style={detail.step}>
                <View style={detail.rail}>
                  <View style={[detail.dot, { backgroundColor: cause ? p.red : p.faint }]} />
                  {i < chain.length - 1 ? <View style={[detail.line, { backgroundColor: p.line }]} /> : null}
                </View>
                <View style={detail.stepBody}>
                  <View style={s.row}>
                    <Text style={[s.small, { color: p.muted, fontFamily: mono }]}>{utcTime(step.timestamp)}</Text>
                    {cause ? <Badge label="cause" tone="red" /> : null}
                  </View>
                  <Text style={[s.body, { color: p.ink, fontWeight: '600' }]}>{step.what}</Text>
                  {step.why_it_matters ? (
                    <Text style={[s.small, { color: p.muted }]}>{step.why_it_matters}</Text>
                  ) : null}
                  <EventIds ids={[step.event_id]} />
                </View>
              </View>
            );
          })}
        </Section>
      ) : null}
    </>
  );
}

function Review({ critic }: { critic: CriticResult }) {
  const p = usePalette();
  const { first, final } = critic;
  const checks = Object.keys(final.checks ?? {}).length;
  const failed = (r: typeof first) => (r.failed ?? []).map((name) => name.replace(/_/g, ' ')).join(', ');
  let text: string;
  if (critic.retried) {
    text = `First review failed (${failed(first)}). The investigator retried and the ${critic.kept ?? 'retry'} report was kept; the second review ${final.verdict === 'pass' ? 'passed' : `failed (${failed(final)})`}.`;
  } else if (final.verdict === 'pass') {
    text = `Reviewer passed ${checks ? `all ${checks} checks` : 'the report'}.`;
  } else {
    text = `Reviewer failed: ${failed(final)}.`;
  }
  return (
    <View style={[s.row, { flexWrap: 'nowrap', alignItems: 'flex-start' }]}>
      <Badge label={`review ${final.verdict}`} tone={final.verdict === 'pass' ? 'green' : 'amber'} />
      <Text style={[s.small, { color: p.muted, flex: 1 }]}>{text}</Text>
    </View>
  );
}

function RemediationSection({ remediation: r }: { remediation: Remediation }) {
  const p = usePalette();
  return (
    <Section
      title="Remediation"
      aside={r.runbook ? <Text style={[s.tiny, { color: p.faint, fontFamily: mono }]}>{r.runbook}</Text> : null}
    >
      {!r.covered ? (
        <Notice tone="amber" title="The runbook does not cover this cause">
          These are safe triage steps, not a fix.
        </Notice>
      ) : null}
      {r.source === 'runbook' ? (
        <Text style={[s.small, { color: p.muted }]}>Steps copied from the runbook (the model step failed).</Text>
      ) : null}
      <Steps title="Do now" items={r.immediate} numbered />
      <Steps title="Verify" items={r.verify} />
      <Steps title="Prevent" items={r.prevent} />
      {r.rollback_risk ? (
        <Notice tone="amber" title="Rollback risk">
          {r.rollback_risk}
        </Notice>
      ) : null}
      {r.note ? <Text style={[s.small, { color: p.muted }]}>{r.note}</Text> : null}
    </Section>
  );
}

function Steps({ title, items, numbered }: { title: string; items: string[]; numbered?: boolean }) {
  const p = usePalette();
  if (!items.length) return null;
  return (
    <View style={{ gap: 6 }}>
      <Text style={[s.small, { color: p.ink, fontWeight: '700' }]}>{title}</Text>
      {items.map((item, i) => (
        <View key={i} style={detail.item}>
          <Text style={[s.body, detail.marker, { color: p.faint }]}>{numbered ? `${i + 1}.` : '•'}</Text>
          <Code text={item} style={{ flex: 1 }} />
        </View>
      ))}
    </View>
  );
}

function Context({ report }: { report: Report }) {
  const p = usePalette();
  const rejected = report.rejected_candidates ?? [];
  const questions = report.open_questions ?? [];
  const affected = report.affected ?? {};
  return (
    <>
      {rejected.length ? (
        <Section title="Ruled out">
          {rejected.map((c, i) => (
            <View key={i} style={{ gap: 4 }}>
              <Text style={[s.body, { color: p.ink, fontWeight: '600' }]}>{c.what}</Text>
              <Text style={[s.small, { color: p.muted }]}>{c.why_rejected}</Text>
              <EventIds ids={c.event_ids} />
            </View>
          ))}
        </Section>
      ) : null}
      {questions.length ? (
        <Section title="Open questions">
          {questions.map((q, i) => (
            <View key={i} style={detail.item}>
              <Text style={[s.body, detail.marker, { color: p.faint }]}>•</Text>
              <Text style={[s.small, { flex: 1, color: q.startsWith('Reviewer:') ? p.amber : p.ink }]}>{q}</Text>
            </View>
          ))}
        </Section>
      ) : null}
      {Object.keys(affected).length ? (
        <Section title="Affected">
          <Pairs data={affected} />
        </Section>
      ) : null}
    </>
  );
}

function Cost({ job }: { job: Job }) {
  const t = job.totals;
  const tokens = (x?: { prompt_tokens?: number; completion_tokens?: number } | null) =>
    (x?.prompt_tokens ?? 0) + (x?.completion_tokens ?? 0);
  return (
    <Section title="Cost">
      <Pairs
        data={{
          'model calls': t.llm_calls,
          'tool calls': t.tool_calls,
          tokens: `${count(t.prompt_tokens)} in · ${count(t.completion_tokens)} out`,
          'model time': seconds((t.llm_latency_ms ?? 0) / 1000),
          'wall time': job.finished ? seconds(job.finished - job.created) : undefined,
          'before review':
            job.totals_initial && job.critic?.retried
              ? `${count(tokens(job.totals_initial))} tokens, ${seconds(job.initial_seconds)}`
              : undefined,
        }}
      />
    </Section>
  );
}

function ActionHistory({ actions }: { actions: Action[] }) {
  const p = usePalette();
  return (
    <Section title="Actions">
      {[...actions].reverse().map((a, i) => (
        <View key={i} style={{ gap: 2 }}>
          <View style={s.row}>
            <Badge label={a.type} tone={a.type === 'escalate' ? 'amber' : a.type === 'approve' ? 'green' : 'blue'} />
            <Text style={[s.small, { color: p.muted }]}>
              {a.by ?? 'demo'} · {utcDateTime(a.ts)}
              {a.state === 'recorded' ? '' : ` · ${a.state?.replace(/_/g, ' ')}`}
            </Text>
          </View>
          {a.question || a.note ? (
            <Text style={[s.small, { color: p.ink }]}>{a.question || a.note}</Text>
          ) : null}
        </View>
      ))}
    </Section>
  );
}

const detail = StyleSheet.create({
  buttons: { flexDirection: 'row', gap: 10 },
  step: { flexDirection: 'row', gap: 12 },
  rail: { width: 12, alignItems: 'center' },
  dot: { width: 10, height: 10, borderRadius: 5, marginTop: 4 },
  line: { width: 2, flex: 1, marginTop: 4 },
  stepBody: { flex: 1, gap: 3, paddingBottom: 14 },
  item: { flexDirection: 'row', gap: 8 },
  marker: { width: 18, textAlign: 'right' },
});
