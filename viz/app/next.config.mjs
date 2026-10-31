/** @type {import('next').NextConfig} */
const nextConfig = {
  // three.js scenes own WebGL contexts; strict mode's double mount would create them twice in dev.
  reactStrictMode: false,
  devIndicators: { buildActivity: false, appIsrStatus: false },
};

export default nextConfig;
