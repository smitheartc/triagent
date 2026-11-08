import { router, useLocalSearchParams } from 'expo-router';
import { memo, type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, type ScrollViewInstance, StyleSheet, Text, View } from 'react-native';

import type { SSETerminal, TraceEntry } from '../../../contracts';
import { count, seconds, shortModel } from '../../../format';
import { followJob } from '../../../sse';
import { mono, STATUS, usePalette, type Tone } from '../../../theme';
import { Badge, Button, EventIds, Notice, s } from '../../../ui';

/** Live investigation: the job's trace over GET /jobs/{id}/events (replayed, then tailed until done or failed). */
export default function Live() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const p = usePalette();
  const [entries, setEntries] = useState<TraceEntry[]>([]);
  const [end, setEnd] = useState<SSETerminal>();
  const [trouble, setTrouble] = useState<{ message: string; retrying: boolean }>();
  const scroller = useRef<ScrollViewInstance>(null);
  const atBottom = useRef(true);

  useEffect(() => {
    return followJob(id, {
      onOpen: () => setTrouble(undefined),
      onEntry: (entry) => setEntries((xs) => [...xs, entry]),
      onEnd: setEnd,
      onError: (err, retrying) => setTrouble({ message: err.message, retrying }),
    });
  }, [id]);

  const stats = useMemo(() => tally(entries), [entries]);
  const status = end?.status ?? stats.status;
  const tokens = (end?.totals.prompt_tokens ?? stats.promptTokens) + (end?.totals.completion_tokens ?? stats.completionTokens);

  return (
    <View style={{ flex: 1 }}>
      <View style={[live.bar, { borderColor: p.line, backgroundColor: p.card }]}>
        <View style={s.row}>
          {status ? <Badge label={STATUS[status]?.label ?? status} tone={STATUS[status]?.tone ?? 'muted'} /> : null}
          {stats.retry && !end ? <Badge label="retry after review" tone="amber" /> : null}
          {!end && !trouble ? <ActivityIndicator size="small" color={p.blue} /> : null}
          <Text style={[s.small, { color: p.muted, marginLeft: 'auto', fontFamily: mono }]}>{seconds(stats.elapsed)}</Text>
        </View>
        <Text style={[s.small, { color: p.muted }]}>
          {end?.totals.tool_calls ?? stats.toolCalls} tool calls · {end?.totals.llm_calls ?? stats.llmCalls} model calls ·{' '}
          {count(tokens)} tokens
        </Text>
      </View>

      {/* A ScrollView, not a FlatList: a trace is at most a few hundred rows, and virtualization lagged the tail. */}
      <ScrollView
        ref={scroller}
        contentContainerStyle={[s.page, { gap: 8 }]}
        onScroll={({ nativeEvent: n }) => {
          atBottom.current = n.contentOffset.y + n.layoutMeasurement.height >= n.contentSize.height - 120;
        }}
        scrollEventThrottle={100}
        onContentSizeChange={() => atBottom.current && scroller.current?.scrollToEnd({ animated: true })}
      >
        {trouble ? (
          <Notice tone={trouble.retrying ? 'amber' : 'red'} title={trouble.retrying ? 'Reconnecting…' : 'Trace unavailable'}>
            {trouble.message}
          </Notice>
        ) : null}
        {entries.map((entry) => (
          <Entry key={entry.seq} entry={entry} />
        ))}
        {end ? (
          <View style={{ gap: 10, marginTop: 8 }}>
            <Notice
              tone={end.status === 'done' ? 'green' : 'red'}
              title={end.status === 'done' ? 'Investigation finished' : 'Investigation failed'}
            >
              {end.error ??
                `${end.totals.tool_calls ?? 0} tool calls, ${end.totals.llm_calls ?? 0} model calls, ` +
                  `${count(tokens)} tokens, ${seconds((end.totals.llm_latency_ms ?? 0) / 1000)} of model time.`}
            </Notice>
            <Button
              title="Open the incident"
              onPress={() => (router.canGoBack() ? router.back() : router.replace(`/jobs/${id}`))}
            />
          </View>
        ) : null}
      </ScrollView>
    </View>
  );
}

/** Running numbers from the trace, counted the way server/tracelog.py totals() counts them. */
function tally(entries: TraceEntry[]) {
  let status: string | undefined;
  let retry = false;
  let llmCalls = 0;
  let promptTokens = 0;
  let completionTokens = 0;
  let toolCalls = 0;
  const calls = new Set<string>();
  for (const e of entries) {
    if (e.kind === 'status' && e.status) {
      status = e.status;
      retry = e.retry === true;
    } else if (e.kind === 'llm') {
      llmCalls++;
      promptTokens += e.prompt_tokens ?? 0;
      completionTokens += e.completion_tokens ?? 0;
    } else if (e.kind === 'tool') {
      const key = `${e.phase}:${e.call_no}`;
      if (e.call_no == null || !calls.has(key)) toolCalls++;
      if (e.call_no != null) calls.add(key);
    }
  }
  const first = entries[0] ? Date.parse(entries[0].ts) : 0;
  const last = entries.length ? Date.parse(entries[entries.length - 1].ts) : 0;
  return { status, retry, llmCalls, promptTokens, completionTokens, toolCalls, elapsed: (last - first) / 1000 };
}

const strings = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);

const FINDING_TONE: Record<string, Tone> = { candidate_cause: 'red', precursor: 'amber', rejected: 'muted' };

const Entry = memo(function Entry({ entry: e }: { entry: TraceEntry }) {
  const p = usePalette();
  switch (e.kind) {
    case 'status': {
      const label = STATUS[e.status ?? '']?.label ?? e.status ?? 'status';
      return (
        <View style={live.divider}>
          <View style={[live.rule, { backgroundColor: p.line }]} />
          <Text style={[s.tiny, { color: p.muted, fontWeight: '700' }]}>
            {label.toUpperCase()}
            {e.retry ? ' (RETRY)' : ''}
          </Text>
          <View style={[live.rule, { backgroundColor: p.line }]} />
        </View>
      );
    }
    case 'llm':
      return (
        <Line tag="model" tone="muted">
          {shortModel(e.model)} · {count(e.prompt_tokens)} in / {count(e.completion_tokens)} out ·{' '}
          {seconds((e.latency_ms ?? 0) / 1000)}
          {e.tool_name ? ` → ${e.tool_name}` : ''}
        </Line>
      );
    case 'tool':
      return <ToolCall entry={e} />;
    case 'finding':
      return (
        <Line
          tag={(e.finding_kind ?? 'finding').replace(/_/g, ' ')}
          tone={FINDING_TONE[e.finding_kind ?? ''] ?? 'blue'}
          below={<EventIds ids={e.event_ids} />}
        >
          {e.finding}
        </Line>
      );
    case 'verify': {
      const dropped = strings(e.dropped);
      const unverified = strings(e.unverified);
      return (
        <Line tag="verify" tone={dropped.length || unverified.length ? 'amber' : 'green'}>
          checked {String(e.checked ?? '?')} cited event ids
          {dropped.length ? `, dropped ${dropped.length}` : ''}
          {unverified.length ? `, ${unverified.length} unverified` : ''}
        </Line>
      );
    }
    case 'assistant':
      return (
        <Line tag="agent" tone="blue">
          <Text style={{ fontStyle: 'italic' }}>{e.text}</Text>
        </Line>
      );
    case 'critic': {
      const failed = strings(e.failed);
      const corrections = strings(e.corrections);
      return (
        <Line tag="review" tone={e.verdict === 'pass' ? 'green' : 'amber'}>
          {e.verdict === 'pass' ? 'pass' : `fail: ${failed.join(', ').replace(/_/g, ' ')}`}
          {corrections.map((c, i) => (
            <Text key={i} style={{ color: p.muted }}>{`\n→ ${c}`}</Text>
          ))}
        </Line>
      );
    }
    case 'remediation':
      return (
        <Line tag="remediation" tone="green">
          {String(e.runbook ?? 'runbook')} · {String(e.steps ?? '?')} steps
          {e.covered === false ? ' · not covered by the runbook' : ''}
          {e.source === 'runbook' ? ' · copied from the runbook' : ''}
        </Line>
      );
    case 'action': {
      const a = (e.action ?? {}) as { type?: string; note?: string; question?: string; state?: string };
      return (
        <Line tag="action" tone="blue">
          {a.type}
          {a.question || a.note ? `: ${a.question || a.note}` : ''}
          {a.state && a.state !== 'recorded' ? ` (${a.state.replace(/_/g, ' ')})` : ''}
        </Line>
      );
    }
    case 'error':
      return (
        <Line tag="error" tone="red">
          {e.message}
        </Line>
      );
    default:
      return (
        <Line tag={e.kind} tone="muted">
          {e.message ?? e.text ?? ''}
        </Line>
      );
  }
});

function Line({ tag, tone, children, below }: { tag: string; tone: Tone; children: ReactNode; below?: ReactNode }) {
  const p = usePalette();
  return (
    <View style={live.line}>
      <Text style={[live.tag, { color: tone === 'muted' ? p.faint : p[tone] }]}>{tag.toUpperCase()}</Text>
      <View style={{ flex: 1, gap: 4 }}>
        <Text style={[s.small, { color: p.ink }]} selectable>
          {children}
        </Text>
        {below}
      </View>
    </View>
  );
}

function ToolCall({ entry: e }: { entry: TraceEntry }) {
  const p = usePalette();
  const [open, setOpen] = useState(false);
  return (
    <Pressable onPress={() => setOpen((o) => !o)} style={[live.tool, { borderColor: p.line, backgroundColor: p.card }]}>
      <View style={s.row}>
        <Text style={[live.tag, { color: p.blue, width: undefined }]}>TOOL #{e.call_no ?? '?'}</Text>
        <Text style={[s.small, { color: p.ink, fontFamily: mono, fontWeight: '700' }]}>{e.name}</Text>
        <Text style={[s.tiny, { color: p.faint, marginLeft: 'auto' }]}>{open ? 'less' : 'more'}</Text>
      </View>
      {e.arguments ? (
        <Text style={[s.tiny, { color: p.muted, fontFamily: mono }]} numberOfLines={open ? undefined : 2}>
          {JSON.stringify(e.arguments)}
        </Text>
      ) : null}
      {e.result ? (
        <Text
          style={[s.tiny, live.result, { color: p.muted, backgroundColor: p.chip, fontFamily: mono }]}
          numberOfLines={open ? undefined : 3}
          selectable={open}
        >
          {e.result}
        </Text>
      ) : null}
    </Pressable>
  );
}

const live = StyleSheet.create({
  bar: { paddingHorizontal: 16, paddingVertical: 10, gap: 4, borderBottomWidth: StyleSheet.hairlineWidth },
  divider: { flexDirection: 'row', alignItems: 'center', gap: 8, marginVertical: 6 },
  rule: { flex: 1, height: StyleSheet.hairlineWidth },
  line: { flexDirection: 'row', gap: 10, alignItems: 'flex-start' },
  tag: { width: 92, fontSize: 10, lineHeight: 18, fontWeight: '700', letterSpacing: 0.5 },
  tool: { borderRadius: 10, borderWidth: StyleSheet.hairlineWidth, padding: 10, gap: 6 },
  result: { borderRadius: 6, padding: 6 },
});
