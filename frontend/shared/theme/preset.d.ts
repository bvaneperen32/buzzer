// Hand-written type for preset.js. It cannot import tailwindcss's Config type: frontend/shared/
// has no node_modules. A .ts preset fails `tsc -b` or emits stray output here, which is why the
// preset is .js plus this file (docs/plans/t9-ui-theming.md section 2.3). Keep it in step with
// preset.js, which may contain only static objects of strings.
declare const preset: { theme: Record<string, Record<string, string>> };
export default preset;
