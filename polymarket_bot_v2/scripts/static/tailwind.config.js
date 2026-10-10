/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './scripts/static/index.html',
    './scripts/static/map.html',
    './scripts/static/js/**/*.js',
  ],
  theme: {
    extend: {
      colors: {
        dark: '#0f1117',
        card: '#1a1d29',
        border: '#2a2d3a',
        accent: '#3b82f6',
        green: '#22c55e',
        red: '#ef4444',
        yellow: '#eab308',
        cyan: '#06b6d4',
      },
    },
  },
  plugins: [],
};
