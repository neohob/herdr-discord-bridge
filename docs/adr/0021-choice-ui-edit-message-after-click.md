# Choice UI edits the message after a successful click

On a successful Operator choice, the Bot edits the component message to a short outcome line and clears the view so buttons cannot be pressed again. Failures use ephemeral errors and may keep the components for retry. Rejected deleting the prompt (loses history) and leaving buttons armed (double-submit).
