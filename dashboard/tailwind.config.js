/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg:           'var(--bg)',
        'bg-elev':    'var(--bg-elev)',
        'bg-hi':      'var(--bg-hi)',
        border:       'var(--border)',
        text:         'var(--text)',
        'text-muted': 'var(--text-muted)',
        accent:       'var(--accent)',
        green:        'var(--green)',
        yellow:       'var(--yellow)',
        red:          'var(--red)',
        purple:       'var(--purple)',
      },
      fontFamily: {
        mono: ['var(--font)'],
      },
      borderRadius: {
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)',
      },
      spacing: {
        1: 'var(--space-1)',
        2: 'var(--space-2)',
        3: 'var(--space-3)',
        4: 'var(--space-4)',
        6: 'var(--space-6)',
        8: 'var(--space-8)',
      },
      transitionDuration: {
        fast: '100ms',
        base: '150ms',
        slow: '300ms',
      },
      boxShadow: {
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)',
      },
    },
  },
  plugins: [],
};
