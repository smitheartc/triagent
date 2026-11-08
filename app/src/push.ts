import { isRunningInExpoGo } from 'expo';
import Constants from 'expo-constants';
import { router } from 'expo-router';
import type { NotificationResponse } from 'expo-notifications';
import { Platform } from 'react-native';

import { api } from './api';

// Expo Go dropped remote push on Android in SDK 53 (a development build has it), and web push is not set up.
const SUPPORTED = Platform.OS === 'ios' || (Platform.OS === 'android' && !isRunningInExpoGo());

/**
 * Registers this device's Expo push token with POST /devices and opens Incident detail when a notification is
 * tapped (PushMessage.data.job_id). Best effort: anything missing (permission, EAS projectId) only logs.
 */
export function setUpPush(): () => void {
  if (!SUPPORTED) return () => {};
  let subscription: { remove(): void } | undefined;
  let stopped = false;

  import('expo-notifications')
    .then(async (Notifications) => {
      if (stopped) return;
      Notifications.setNotificationHandler({
        handleNotification: async () => ({
          shouldShowBanner: true,
          shouldShowList: true,
          shouldPlaySound: true,
          shouldSetBadge: false,
        }),
      });
      const open = (response: NotificationResponse | null) => {
        const jobId = response?.notification.request.content.data?.job_id;
        if (typeof jobId === 'string') router.push(`/jobs/${jobId}`);
      };
      open(Notifications.getLastNotificationResponse());
      subscription = Notifications.addNotificationResponseReceivedListener(open);

      // Without an EAS project there is no token to get, so don't ask for permission at all.
      const projectId = Constants.easConfig?.projectId ?? Constants.expoConfig?.extra?.eas?.projectId;
      if (!projectId) {
        console.info('push: no EAS projectId in app config (npx eas-cli@latest init adds one); not registering');
        return;
      }
      if (Platform.OS === 'android') {
        await Notifications.setNotificationChannelAsync('default', {
          name: 'Incidents',
          importance: Notifications.AndroidImportance.HIGH,
        });
      }
      let { status } = await Notifications.getPermissionsAsync();
      if (status !== 'granted') ({ status } = await Notifications.requestPermissionsAsync());
      if (status !== 'granted') return;
      const token = await Notifications.getExpoPushTokenAsync({ projectId });
      await api.registerDevice({ expo_push_token: token.data, platform: Platform.OS === 'ios' ? 'ios' : 'android' });
    })
    .catch((err) => console.warn('push: not registered:', err instanceof Error ? err.message : err));

  return () => {
    stopped = true;
    subscription?.remove();
  };
}
