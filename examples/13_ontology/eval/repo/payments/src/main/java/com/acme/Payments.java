class Payments {
  @KafkaListener(topics = "order.events", groupId = "payments")
  void onOrder(String order) {
    kafkaTemplate.send("payment.events", order);
  }
}
