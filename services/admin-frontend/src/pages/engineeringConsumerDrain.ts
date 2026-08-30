type EngineeringConsumerApi = {
  post: <T>(path: string, body: unknown) => Promise<T>
  delete: <T>(path: string) => Promise<T>
}

export function requestEngineeringConsumerDrain(api: EngineeringConsumerApi) {
  return api.post('/engineering-consumer/drain', {})
}

export function requestEngineeringConsumerResume(api: EngineeringConsumerApi) {
  return api.delete('/engineering-consumer/drain')
}
