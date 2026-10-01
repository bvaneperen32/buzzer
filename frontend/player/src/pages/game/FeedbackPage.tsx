export default function FeedbackPage() {
  return (
    <div className="min-h-screen flex flex-col items-center justify-center p-6 gap-6 text-center">
      <div className="text-6xl">🔒</div>
      <p className="text-primary text-2xl font-bold">Answer locked in!</p>
      <div className="flex justify-center mt-2">
        <div className="w-8 h-8 border-4 border-accent border-t-transparent rounded-full animate-spin" />
      </div>
      <p className="text-secondary text-sm">Waiting for host to reveal results…</p>
    </div>
  );
}
