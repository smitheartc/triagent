import type { ConfigContext, ExpoConfig } from 'expo/config';

/**
 * app.json plus the Firebase config Android needs for push (FCM), from the GOOGLE_SERVICES_JSON variable. EAS builds
 * get it as a file variable (`npx eas-cli env:set --environment preview --name GOOGLE_SERVICES_JSON --type file
 * --value ./google-services.json --visibility secret`); for a local build, `GOOGLE_SERVICES_JSON=./google-services.json`.
 * The file is never committed. Without it the app still builds; Android just gets no push token.
 */
export default ({ config }: ConfigContext): ExpoConfig => {
  const googleServicesFile = process.env.GOOGLE_SERVICES_JSON;
  return {
    ...(config as ExpoConfig),
    android: { ...config.android, ...(googleServicesFile ? { googleServicesFile } : {}) },
  };
};
