Fault types (use these exact names; you may propose only: $fault_types):
- SOILING: dust on the panels after a dust storm. The whole plant produces less than the trusted weather reference predicts; high wind came first; the loss stays after the wind drops. Target: ALL.
- SENSOR_SOILING: a dusty irradiance sensor reads low. The sensor disagrees with the reference and the clear-sky shape on clear periods. It hides panel soiling, because output divided by the sensor looks normal. Target: the weather station.
- INVERTER_DERATE: one inverter is capped flat while its peers are not, its internal temperature is high, and it raises a derate alarm. Target: that inverter.
- COMMS_DROPOUT: rows are missing for one device (missing, not zero) while the meter shows it still producing. Target: that device.
- CURTAILMENT: every inverter capped together while a grid setpoint is present. This is expected behaviour, not a fault; propose it only to explain such a cap. Target: ALL.
- TRACKER_STUCK: one tracker block stays at a flat angle after the wind has dropped, losing output mostly in the morning and evening. Stowing during high wind is normal. Target: that inverter.
- STRING_OPEN: one inverter's DC input drops by a fixed fraction with no link to the weather. Target: that inverter.
- UNAUTHORIZED_CMD: an inverter drops to zero right after a control command from an unexpected source or time, with no electrical alarm before it. Target: that inverter.
