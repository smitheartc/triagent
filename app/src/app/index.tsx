import { router, Stack } from 'expo-router';
import { FlatList, Platform, Pressable, RefreshControl, StyleSheet, Text, View } from 'react-native';

import { API_URL } from '../api';
import type { JobSummary } from '../contracts';
import { ago, pct } from '../format';
import { mono, usePalette } from '../theme';
import { Badge, Empty, Loading, Notice, PriorityBadge, s, StatusBadge } from '../ui';
import { type InboxSource, useInbox } from '../useInbox';

/** Inbox, newest first: Supabase with a realtime subscription when configured, else GET /jobs every 3 s. */
export default function Inbox() {
  const p = usePalette();
  const { jobs, error, source, refresh, refreshing } = useInbox();

  return (
    <>
      <Stack.Screen
        options={{
          headerRight: () => (
            <Pressable
              onPress={() => router.push('/scores')}
              hitSlop={12}
              accessibilityRole="link"
              style={{ marginRight: Platform.OS === 'web' ? 16 : 0 }}
            >
              <Text style={[s.body, { color: p.accent, fontWeight: '600' }]}>Scoreboard</Text>
            </Pressable>
          ),
        }}
      />
      <FlatList
        data={jobs ?? []}
        keyExtractor={(job) => job.job_id}
        renderItem={({ item }) => <JobRow job={item} />}
        ItemSeparatorComponent={() => <View style={{ height: 10 }} />}
        contentContainerStyle={s.page}
        refreshControl={<RefreshControl refreshing={refreshing} onRefresh={refresh} tintColor={p.muted} />}
        ListHeaderComponent={
          error ? (
            <View style={{ marginBottom: 10 }}>
              <Notice tone="red" title="Can't load incidents">
                {`${error.message}\nServer: ${API_URL}. Start server/fake_server.py, or set EXPO_PUBLIC_API_URL.`}
              </Notice>
            </View>
          ) : jobs ? (
            <SourceLine source={source} />
          ) : undefined
        }
        ListEmptyComponent={
          jobs ? (
            <Empty title="No incidents yet" detail="Tickets arrive from Jira or jira_mock through POST /webhook." />
          ) : error ? undefined : (
            <Loading />
          )
        }
      />
    </>
  );
}

function JobRow({ job }: { job: JobSummary }) {
  const p = usePalette();
  return (
    <Pressable
      onPress={() => router.push(`/jobs/${job.job_id}`)}
      accessibilityRole="button"
      style={({ pressed }) => [row.card, { backgroundColor: p.card, borderColor: p.line, opacity: pressed ? 0.8 : 1 }]}
    >
      <View style={s.row}>
        <PriorityBadge priority={job.priority} />
        <Text style={[s.small, { color: p.muted, fontFamily: mono }]}>{job.ticket_id}</Text>
        {job.service ? <Text style={[s.small, { color: p.muted }]}>{job.service}</Text> : null}
        <Text style={[s.small, { color: p.faint, marginLeft: 'auto' }]}>{ago(job.created)}</Text>
      </View>
      <Text style={[s.body, { color: p.ink, fontWeight: '600' }]} numberOfLines={2}>
        {job.title}
      </Text>
      <View style={s.row}>
        <StatusBadge status={job.status} />
        {job.category ? <Badge label={job.category} /> : null}
        {job.entity ? <Text style={[s.small, { color: p.ink, fontFamily: mono }]}>{job.entity}</Text> : null}
        {job.confidence != null ? (
          <Text style={[s.small, { color: p.muted }]}>{pct(job.confidence)} confidence</Text>
        ) : null}
        {job.critic_verdict === 'fail' ? <Badge label="reviewer flagged" tone="amber" /> : null}
      </View>
    </Pressable>
  );
}

const row = StyleSheet.create({
  card: { borderRadius: 12, borderWidth: StyleSheet.hairlineWidth, padding: 14, gap: 8 },
});

const SOURCE_TEXT: Record<InboxSource, string> = {
  realtime: 'Live: updates from Supabase as they happen',
  supabase: 'From Supabase, connecting to live updates…',
  server: `From the server, every 3 s`,
};

/** One line above the list saying where the rows come from, so a stale phone is easy to spot. */
function SourceLine({ source }: { source: InboxSource }) {
  const p = usePalette();
  const color = source === 'realtime' ? p.green : p.muted;
  return (
    <View style={[s.row, { marginBottom: 10 }]}>
      <View style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: color }} />
      <Text style={[s.small, { color: p.muted }]}>{SOURCE_TEXT[source]}</Text>
    </View>
  );
}
