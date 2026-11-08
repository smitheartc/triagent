import { DarkTheme, DefaultTheme, Stack, ThemeProvider } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { useEffect } from 'react';
import { useColorScheme } from 'react-native';

import { setUpPush } from '../push';
import { palettes } from '../theme';

export default function RootLayout() {
  const dark = useColorScheme() === 'dark';
  const p = dark ? palettes.dark : palettes.light;
  const base = dark ? DarkTheme : DefaultTheme;

  useEffect(() => setUpPush(), []);

  return (
    <ThemeProvider
      value={{
        ...base,
        colors: { ...base.colors, primary: p.accent, background: p.bg, card: p.bg, text: p.ink, border: p.line },
      }}
    >
      <Stack screenOptions={{ headerShadowVisible: false, headerTitleStyle: { fontWeight: '600' } }}>
        <Stack.Screen name="index" options={{ title: 'Incidents' }} />
        <Stack.Screen name="scores" options={{ title: 'Scoreboard' }} />
        <Stack.Screen name="jobs/[id]/index" options={{ title: 'Incident' }} />
        <Stack.Screen name="jobs/[id]/live" options={{ title: 'Live investigation' }} />
        <Stack.Screen name="jobs/[id]/actions" options={{ title: 'Actions' }} />
      </Stack>
      <StatusBar style="auto" />
    </ThemeProvider>
  );
}
