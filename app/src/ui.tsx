import type { ReactNode } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';

import { mono, priorityTone, STATUS, tint, usePalette, type Tone } from './theme';

export function Badge({ label, tone = 'muted' }: { label: string; tone?: Tone }) {
  const p = usePalette();
  return (
    <View style={[s.badge, { backgroundColor: tone === 'muted' ? p.chip : tint(p[tone]) }]}>
      <Text style={[s.badgeText, { color: p[tone] }]}>{label}</Text>
    </View>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const st = STATUS[status] ?? { label: status, tone: 'muted' as const };
  return <Badge label={st.label} tone={st.tone} />;
}

export function PriorityBadge({ priority }: { priority: string }) {
  return <Badge label={priority || 'P?'} tone={priorityTone(priority)} />;
}

export function Section({ title, aside, children }: { title: string; aside?: ReactNode; children: ReactNode }) {
  const p = usePalette();
  return (
    <View style={[s.section, { backgroundColor: p.card, borderColor: p.line }]}>
      <View style={s.sectionHead}>
        <Text style={[s.sectionTitle, { color: p.muted }]}>{title}</Text>
        {aside}
      </View>
      {children}
    </View>
  );
}

/** Text with `backtick` spans rendered as code (remediation steps quote commands this way). */
export function Code({ text, style }: { text: string; style?: object }) {
  const p = usePalette();
  return (
    <Text style={[s.body, { color: p.ink }, style]} selectable>
      {text.split('`').map((part, i) =>
        i % 2 ? (
          <Text key={i} style={[s.code, { backgroundColor: p.chip, color: p.ink }]}>
            {part}
          </Text>
        ) : (
          part
        ),
      )}
    </Text>
  );
}

export function EventIds({ ids }: { ids?: string[] | null }) {
  const p = usePalette();
  if (!ids?.length) return null;
  return (
    <View style={s.ids}>
      {ids.map((id) => (
        <Text key={id} style={[s.id, { color: p.muted, backgroundColor: p.chip }]} selectable>
          {id}
        </Text>
      ))}
    </View>
  );
}

export function Meter({ value, tone = 'green' }: { value: number; tone?: Tone }) {
  const p = usePalette();
  return (
    <View style={[s.meter, { backgroundColor: p.chip }]}>
      <View style={[s.meterFill, { width: `${Math.round(value * 100)}%`, backgroundColor: p[tone] }]} />
    </View>
  );
}

export function Notice({ tone, title, children }: { tone: Tone; title: string; children?: ReactNode }) {
  const p = usePalette();
  return (
    <View style={[s.notice, { borderColor: p[tone], backgroundColor: tint(p[tone]) }]}>
      <Text style={[s.noticeTitle, { color: p[tone] }]}>{title}</Text>
      {typeof children === 'string' ? <Text style={[s.small, { color: p.ink }]}>{children}</Text> : children}
    </View>
  );
}

export function Button({
  title,
  onPress,
  kind = 'primary',
  disabled,
  busy,
}: {
  title: string;
  onPress: () => void;
  kind?: 'primary' | 'plain';
  disabled?: boolean;
  busy?: boolean;
}) {
  const p = usePalette();
  const primary = kind === 'primary';
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled || busy}
      accessibilityRole="button"
      style={({ pressed }) => [
        s.button,
        primary ? { backgroundColor: p.accent } : { borderColor: p.line, borderWidth: StyleSheet.hairlineWidth * 2 },
        (pressed || disabled) && { opacity: disabled ? 0.45 : 0.75 },
      ]}
    >
      {busy ? (
        <ActivityIndicator color={primary ? p.onAccent : p.ink} />
      ) : (
        <Text style={[s.buttonText, { color: primary ? p.onAccent : p.ink }]}>{title}</Text>
      )}
    </Pressable>
  );
}

export function Empty({ title, detail }: { title: string; detail?: string }) {
  const p = usePalette();
  return (
    <View style={s.empty}>
      <Text style={[s.emptyTitle, { color: p.ink }]}>{title}</Text>
      {detail ? <Text style={[s.small, { color: p.muted, textAlign: 'center' }]}>{detail}</Text> : null}
    </View>
  );
}

export function Loading() {
  const p = usePalette();
  return (
    <View style={s.empty}>
      <ActivityIndicator color={p.muted} />
    </View>
  );
}

/** Key/value rows for small objects (identifiers, affected, totals). */
export function Pairs({ data }: { data: Record<string, unknown> }) {
  const p = usePalette();
  const rows = Object.entries(data).filter(([, v]) => v != null && v !== '');
  if (!rows.length) return null;
  return (
    <View style={s.pairs}>
      {rows.map(([k, v]) => (
        <View key={k} style={s.pair}>
          <Text style={[s.small, s.pairKey, { color: p.muted }]}>{k.replace(/_/g, ' ')}</Text>
          <Text style={[s.small, s.pairValue, { color: p.ink }]} selectable>
            {Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : String(v)}
          </Text>
        </View>
      ))}
    </View>
  );
}

export const s = StyleSheet.create({
  page: { padding: 16, paddingBottom: 40, gap: 12, width: '100%', maxWidth: 760, alignSelf: 'center' },
  title: { fontSize: 20, lineHeight: 26, fontWeight: '600' },
  body: { fontSize: 15, lineHeight: 22 },
  small: { fontSize: 13, lineHeight: 18 },
  tiny: { fontSize: 11, lineHeight: 15 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  badge: { borderRadius: 999, paddingHorizontal: 8, paddingVertical: 2, alignSelf: 'flex-start' },
  badgeText: { fontSize: 12, lineHeight: 16, fontWeight: '600' },
  section: { borderRadius: 12, borderWidth: StyleSheet.hairlineWidth, padding: 14, gap: 10 },
  sectionHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  sectionTitle: { fontSize: 12, fontWeight: '700', letterSpacing: 0.6, textTransform: 'uppercase' },
  code: { fontFamily: mono, fontSize: 13 },
  ids: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  id: { fontFamily: mono, fontSize: 11, lineHeight: 15, borderRadius: 4, paddingHorizontal: 5, paddingVertical: 2 },
  meter: { height: 6, borderRadius: 3, overflow: 'hidden', flex: 1 },
  meterFill: { height: 6, borderRadius: 3 },
  notice: { borderLeftWidth: 3, borderRadius: 8, padding: 12, gap: 4 },
  noticeTitle: { fontSize: 14, fontWeight: '600' },
  button: { borderRadius: 10, paddingVertical: 12, paddingHorizontal: 16, alignItems: 'center', minHeight: 46 },
  buttonText: { fontSize: 15, fontWeight: '600' },
  empty: { padding: 32, alignItems: 'center', gap: 6 },
  emptyTitle: { fontSize: 16, fontWeight: '600', textAlign: 'center' },
  pairs: { gap: 4 },
  pair: { flexDirection: 'row', gap: 12 },
  pairKey: { width: 110 },
  pairValue: { flex: 1 },
});
