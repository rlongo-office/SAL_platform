import numpy as np
from scipy.stats import norm, t

def compute_mean_std(sample):
    """Returns the mean and standard deviation of a given sample."""
    mean = np.mean(sample)
    std_dev = np.std(sample, ddof=1)  # Using sample standard deviation (Bessel's correction)
    return mean, std_dev

def hypothesis_test(sample, expected_mean, confidence=0.99, tail='two'):
    """
    Performs a hypothesis test (Z-Test if n >= 30, T-Test if n < 30).
    
    Parameters:
        - sample: list or numpy array of sample data
        - expected_mean: the expected population mean under H0
        - confidence: confidence level (default 99%)
        - tail: 'left', 'right', or 'two' (default 'two')
    
    Returns:
        - test_stat: Z-score or T-score
        - p_value: probability of obtaining the observed data under H0
        - decision: whether to reject or fail to reject H0
    """
    n = len(sample)
    mean, std_dev = compute_mean_std(sample)
    std_error = std_dev / np.sqrt(n)
    
    # Choose test type
    if n >= 30:
        test_stat = (mean - expected_mean) / std_error
        p_value = (1 - norm.cdf(abs(test_stat))) * (2 if tail == 'two' else 1)
        test_type = "Z-Test"
    else:
        test_stat = (mean - expected_mean) / std_error
        p_value = (1 - t.cdf(abs(test_stat), df=n-1)) * (2 if tail == 'two' else 1)
        test_type = "T-Test"
    
    # Determine rejection threshold
    alpha = 1 - confidence
    if tail == 'two':
        critical_value = norm.ppf(1 - alpha / 2) if n >= 30 else t.ppf(1 - alpha / 2, df=n-1)
        reject = abs(test_stat) > critical_value
    elif tail == 'left':
        critical_value = norm.ppf(alpha) if n >= 30 else t.ppf(alpha, df=n-1)
        reject = test_stat < critical_value
    else:  # Right-tailed test
        critical_value = norm.ppf(1 - alpha) if n >= 30 else t.ppf(1 - alpha, df=n-1)
        reject = test_stat > critical_value
    
    decision = "Reject H0" if reject else "Fail to Reject H0"
    
    return {
        "test_type": test_type,
        "test_stat": round(test_stat, 4),
        "p_value": round(p_value, 6),
        "decision": decision
    }
