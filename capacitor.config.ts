import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.itamar.raildashboard',
  appName: 'Mesilot',
  webDir: 'web',
  server: {
    url: 'https://mesilot.vercel.app',
    cleartext: false
  }
};

export default config;

