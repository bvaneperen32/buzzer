import { cn } from '../../lib/utils';

type InputProps = React.InputHTMLAttributes<HTMLInputElement>;

export function Input({ className, ...props }: InputProps) {
  return (
    <input
      className={cn(
        'w-full rounded-lg border border-input bg-raised px-3 py-2',
        'text-primary placeholder:text-secondary',
        'focus-visible:border-accent',
        className
      )}
      {...props}
    />
  );
}
