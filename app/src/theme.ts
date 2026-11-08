import { Platform, useColorScheme } from 'react-native';

const light = {
  bg: '#F5F4F0',
  card: '#FFFFFF',
  ink: '#17191C',
  muted: '#5C626B',
  faint: '#8B9099',
  line: '#E4E2DC',
  chip: '#EEECE6',
  accent: '#3D6B00',
  onAccent: '#FFFFFF',
  blue: '#1D5FD1',
  green: '#1E7B3A',
  amber: '#A15C00',
  red: '#B42318',
};

const dark: typeof light = {
  bg: '#0F1113',
  card: '#181B1F',
  ink: '#ECEDEE',
  muted: '#A3A9B1',
  faint: '#737A83',
  line: '#2A2F35',
  chip: '#22262B',
  accent: '#8FD14F',
  onAccent: '#0F1113',
  blue: '#7AA7FF',
  green: '#5FD38A',
  amber: '#F5B84F',
  red: '#FF7A6E',
};

export type Palette = typeof light;
export type Tone = 'blue' | 'green' | 'amber' | 'red' | 'muted';

export const palettes = { light, dark };

export function usePalette(): Palette {
  return useColorScheme() === 'dark' ? dark : light;
}

export const mono = Platform.select({
  ios: 'Menlo',
  android: 'monospace',
  default: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
});

/** Tinted background for a tone color (#RRGGBB -> #RRGGBBAA). */
export const tint = (color: string) => `${color}22`;

export const STATUS: Record<string, { label: string; tone: Tone }> = {
  queued: { label: 'queued', tone: 'muted' },
  running: { label: 'investigating', tone: 'blue' },
  critic: { label: 'reviewing', tone: 'blue' },
  remediating: { label: 'remediating', tone: 'blue' },
  done: { label: 'done', tone: 'green' },
  failed: { label: 'failed', tone: 'red' },
};

export function priorityTone(priority: string): Tone {
  if (priority === 'P1') return 'red';
  if (priority === 'P2') return 'amber';
  return 'muted';
}
