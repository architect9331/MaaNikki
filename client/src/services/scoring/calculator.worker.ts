import { calculate } from './optimizer';
import type { CalculationRequest } from './types';

self.onmessage = (event: MessageEvent<CalculationRequest>) => {
  try {
    const result = calculate(event.data, (stage) => self.postMessage({ stage }));
    self.postMessage({ result });
  } catch (error) {
    self.postMessage({ error: error instanceof Error ? error.message : String(error) });
  }
};
