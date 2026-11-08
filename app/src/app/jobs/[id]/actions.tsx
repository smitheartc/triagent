import { useLocalSearchParams } from 'expo-router';
import { useEffect, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';

import { api } from '../../../api';
import type { Action, ActionRequest } from '../../../contracts';
import { utcDateTime } from '../../../format';
import { mono, usePalette } from '../../../theme';
import { Badge, Button, Loading, Notice, s } from '../../../ui';
import { usePoll } from '../../../usePoll';

type Kind = ActionRequest['type'];

const KINDS: { type: Kind; label: string; button: string; placeholder: string }[] = [
  { type: 'approve', label: 'Approve', button: 'Approve remediation', placeholder: 'Note for the ticket (optional)' },
  { type: 'escalate', label: 'Escalate', button: 'Escalate', placeholder: 'Who should look at this, and why' },
  { type: 'ask', label: 'Ask', button: 'Ask the agent', placeholder: 'A follow-up question about this incident' },
];

/** Actions: approve remediation or escalate (POST /jobs/{id}/actions), or ask a follow-up (POST /jobs/{id}/ask). */
export default function Actions() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const p = usePalette();
  const { data: job, error, refresh } = usePoll(() => api.job(id), () => false);
  const [kind, setKind] = useState<Kind>('approve');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string }>();
  const current = KINDS.find((k) => k.type === kind)!;
  // While a follow-up is open, refresh every 2 s until it is answered or failed.
  const pending = job?.actions?.some((a) => a.type === 'ask' && (a.state === 'queued' || a.state === 'answering'));
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(refresh, 2000);
    return () => clearInterval(timer);
  }, [pending, refresh]);

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

  async function submit() {
    setBusy(true);
    setResult(undefined);
    try {
      const value = text.trim();
      const action =
        kind === 'ask' ? await api.ask(id, { question: value }) : await api.act(id, { type: kind, note: value || null });
      setResult({ ok: true, message: outcome(action) });
      setText('');
      refresh();
    } catch (err) {
      setResult({ ok: false, message: err instanceof Error ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  const steps = job.remediation?.immediate.length ?? 0;
  const context = {
    approve: job.remediation
      ? `Approves the ${steps} immediate step${steps === 1 ? '' : 's'} from ${job.remediation.runbook ?? 'the runbook'}.`
      : 'There is no remediation on this incident yet.',
    escalate: 'Hands the incident to a human team with your note.',
    ask: 'The agent answers from its report and findings, and can run a few more queries. Answers cite event ids.',
  }[kind];

  return (
    <ScrollView contentContainerStyle={s.page} keyboardShouldPersistTaps="handled" automaticallyAdjustKeyboardInsets>
      <Text style={[s.small, { color: p.muted }]}>
        {job.ticket_id} · {job.ticket.title}
      </Text>

      <View style={[act.tabs, { backgroundColor: p.chip }]}>
        {KINDS.map((k) => (
          <Pressable
            key={k.type}
            onPress={() => {
              setKind(k.type);
              setResult(undefined);
            }}
            accessibilityRole="tab"
            accessibilityState={{ selected: k.type === kind }}
            style={[act.tab, k.type === kind && { backgroundColor: p.card }]}
          >
            <Text style={[s.small, { color: k.type === kind ? p.ink : p.muted, fontWeight: '600' }]}>{k.label}</Text>
          </Pressable>
        ))}
      </View>

      <Text style={[s.small, { color: p.muted }]}>{context}</Text>
      <TextInput
        value={text}
        onChangeText={setText}
        placeholder={current.placeholder}
        placeholderTextColor={p.faint}
        multiline
        style={[act.input, s.body, { color: p.ink, borderColor: p.line, backgroundColor: p.card }]}
      />
      <Button
        title={current.button}
        onPress={submit}
        busy={busy}
        disabled={kind === 'ask' && !text.trim()}
      />
      {result ? (
        <Notice tone={result.ok ? 'green' : 'red'} title={result.ok ? 'Sent' : 'Not sent'}>
          {result.message}
        </Notice>
      ) : null}

      {job.actions?.length ? (
        <View style={{ gap: 10, marginTop: 8 }}>
          <Text style={[s.small, { color: p.muted, fontWeight: '700' }]}>On this incident</Text>
          {[...job.actions].reverse().map((a, i) => (
            <View key={a.id ?? i} style={{ gap: 4 }}>
              <View style={s.row}>
                <Badge label={a.type} tone={a.type === 'escalate' ? 'amber' : a.type === 'approve' ? 'green' : 'blue'} />
                {a.type === 'ask' && a.state !== 'answered' ? (
                  <Badge label={a.state ?? 'queued'} tone={a.state === 'failed' ? 'red' : 'amber'} />
                ) : null}
                <Text style={[s.small, { color: p.muted }]}>{utcDateTime(a.ts)}</Text>
              </View>
              {a.question || a.note ? <Text style={[s.small, { color: p.ink }]}>{a.question || a.note}</Text> : null}
              {a.answer ? <Text style={[s.body, { color: p.ink }]}>{a.answer}</Text> : null}
              {a.event_ids?.length ? (
                <Text style={[s.small, { color: p.muted, fontFamily: mono }]} selectable>
                  {a.event_ids.join('\n')}
                </Text>
              ) : null}
              {a.state === 'failed' && a.error ? (
                <Text style={[s.small, { color: p.red }]}>No answer: {a.error}</Text>
              ) : null}
            </View>
          ))}
        </View>
      ) : null}
    </ScrollView>
  );
}

function outcome(action: Action): string {
  if (action.type === 'ask') return 'Asked. The answer appears below when the investigator finishes (usually under a minute).';
  return `Recorded ${action.type} on the incident.`;
}

const act = StyleSheet.create({
  tabs: { flexDirection: 'row', borderRadius: 10, padding: 3 },
  tab: { flex: 1, alignItems: 'center', paddingVertical: 8, borderRadius: 8 },
  input: { minHeight: 96, borderRadius: 10, borderWidth: StyleSheet.hairlineWidth * 2, padding: 12, textAlignVertical: 'top' },
});
